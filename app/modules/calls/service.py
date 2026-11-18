from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import DomainError
from app.core.idempotency import fingerprint, remember, replay
from app.db.models.call import (
    AlertDelivery,
    Call,
    CallEpisode,
    CallSchedule,
    CareContact,
    EmergencyAlert,
)
from app.db.models.call_runtime import CallExecution, OutboxMessage
from app.integrations.koreaaitel.client import DispatchRejected
from app.modules.calls.repository import TERMINAL, require_eligible, senior_for_call


def validate_target(session, payload, emergency, *, dispatch=False):
    if emergency:
        delivery = session.get(AlertDelivery, payload["delivery_id"])
        if delivery is None:
            raise DomainError(404, "CALL_NOT_FOUND", "비상전화 작업을 찾을 수 없습니다.")
        alert = session.get(EmergencyAlert, delivery.alert_id)
        senior = senior_for_call(session, alert.call_id, lock=True)
        session.refresh(alert)
        session.refresh(delivery)
        contact = session.get(CareContact, delivery.contact_id)
        if (
            str(alert.id) != payload["alert_id"]
            or str(contact.id) != payload["contact_id"]
            or contact.senior_id != senior.id
            or contact.phone_number != payload["phone_number"]
            or not contact.enabled
            or contact.deleted_at
            or alert.status not in {"pending", "contacting"}
        ):
            raise DomainError(409, "EXECUTION_MISMATCH", "비상전화 작업과 요청이 다릅니다.")
        command = session.scalar(
            select(OutboxMessage).where(
                OutboxMessage.producer_service == "C",
                OutboxMessage.operation == "emergency.start",
                OutboxMessage.resource_id == delivery.id,
            )
        )
    else:
        senior = senior_for_call(session, payload["call_id"], lock=True)
        call = session.get(Call, payload["call_id"])
        schedule = session.get(CallSchedule, session.get(CallEpisode, call.episode_id).schedule_id)
        if not schedule.enabled or schedule.deleted_at or call.status in TERMINAL:
            raise DomainError(409, "CALL_INACTIVE", "발신할 수 없는 통화입니다.")
        if senior.phone_number != payload["phone_number"]:
            raise DomainError(409, "EXECUTION_MISMATCH", "전화번호가 변경되었습니다.")
        command = session.scalar(
            select(OutboxMessage).where(
                OutboxMessage.producer_service == "C",
                OutboxMessage.operation == "call.start",
                OutboxMessage.resource_id == call.id,
            )
        )
    require_eligible(session, senior)
    if command is None or command.payload != payload:
        raise DomainError(409, "EXECUTION_MISMATCH", "C의 저장된 발신 요청과 일치하지 않습니다.")
    if dispatch:
        # Member lock serializes check-ins and emergency dispatch across workers.
        other = session.scalar(
            select(CallExecution.id)
            .join(Call, CallExecution.call_id == Call.id)
            .join(CallEpisode, Call.episode_id == CallEpisode.id)
            .join(CallSchedule, CallEpisode.schedule_id == CallSchedule.id)
            .where(
                CallSchedule.senior_id == senior.id,
                CallExecution.execution_status.in_(["running", "unknown"]),
            )
            .limit(1)
        )
        emergency_other = session.scalar(
            select(CallExecution.id)
            .join(AlertDelivery, CallExecution.delivery_id == AlertDelivery.id)
            .join(EmergencyAlert, AlertDelivery.alert_id == EmergencyAlert.id)
            .join(Call, EmergencyAlert.call_id == Call.id)
            .join(CallEpisode, Call.episode_id == CallEpisode.id)
            .join(CallSchedule, CallEpisode.schedule_id == CallSchedule.id)
            .where(
                CallSchedule.senior_id == senior.id,
                CallExecution.execution_status.in_(["running", "unknown"]),
            )
            .limit(1)
        )
        if other or emergency_other:
            raise DomainError(409, "CALL_BUSY", "선행 통화 결과를 확인해야 합니다.")
    return senior


def accept(session, subject, data, key, emergency=False):
    payload = data.model_dump(mode="json")
    field = "delivery_id" if emergency else "call_id"
    operation = "emergency.start" if emergency else "call.start"
    with session.begin():
        cached = replay(session, "T", subject, operation, key, payload)
        if cached:
            return cached.response_body
        resource = payload[field]
        # Same business ID under a different key must also be idempotent.
        from app.core.idempotency import lock_key

        lock_key(session, f"T:execute:{field}:{resource}")
        execution = session.scalar(
            select(CallExecution).where(getattr(CallExecution, field) == resource)
        )
        if execution and execution.request_hash != fingerprint(payload):
            raise DomainError(409, "IDEMPOTENCY_CONFLICT", "같은 통화의 실행 내용이 다릅니다.")
        if execution is None:
            # Only an exact C-owned command can be accepted. The worker rechecks
            # eligibility and durably reports cancellation if it changed.
            command = session.scalar(
                select(OutboxMessage).where(
                    OutboxMessage.producer_service == "C",
                    OutboxMessage.operation == operation,
                    OutboxMessage.resource_id == resource,
                )
            )
            if command is None or command.payload != payload:
                raise DomainError(409, "EXECUTION_MISMATCH", "저장된 발신 요청과 다릅니다.")
            if emergency:
                delivery = session.get(AlertDelivery, resource)
                if delivery is None:
                    raise DomainError(404, "CALL_NOT_FOUND", "비상전화 작업이 없습니다.")
                senior_for_call(session, session.get(EmergencyAlert, delivery.alert_id).call_id)
            else:
                senior_for_call(session, resource)
            execution = CallExecution(
                **{field: resource},
                kind="emergency" if emergency else "check_in",
                request_payload=payload,
                request_hash=fingerprint(payload),
            )
            session.add(execution)
        body = {field: resource, "execution_status": "accepted"}
        remember(session, "T", subject, operation, key, payload, 202, body)
        return body


def emit_state(session, execution, status, occurred_at, sequence=None):
    resource = execution.call_id or execution.delivery_id
    event_id = f"state:{uuid4()}"
    session.add(
        OutboxMessage(
            producer_service="T",
            operation="call.event" if execution.call_id else "delivery.event",
            resource_id=resource,
            dedupe_key=event_id,
            available_at=datetime.now(UTC),
            payload={
                "event_id": event_id,
                "status": status,
                "occurred_at": occurred_at.isoformat().replace("+00:00", "Z"),
                "provider_call_id": execution.provider_call_id,
                "provider_sequence": sequence,
            },
        )
    )


def local_failure(session, execution, status, reason, at):
    execution.execution_status, execution.provider_status, execution.last_error = (
        "failed",
        status,
        reason,
    )
    if execution.call_id:
        call = session.get(Call, execution.call_id)
        call.status, call.ended_at = status, at
    emit_state(session, execution, status, at)


def dispatch_one(engine, provider, at=None):
    at = at or datetime.now(UTC)
    token = uuid4()
    with Session(engine) as session, session.begin():
        # A worker crash after claiming may have placed a call. Never redial on lease expiry.
        for expired in session.scalars(
            select(CallExecution)
            .where(
                CallExecution.execution_status == "running",
                CallExecution.provider_call_id.is_(None),
                CallExecution.lease_until < at,
            )
            .with_for_update(skip_locked=True)
        ):
            expired.execution_status, expired.last_error = "unknown", "DISPATCH_OUTCOME_UNKNOWN"
        execution = session.scalar(
            select(CallExecution)
            .where(CallExecution.execution_status == "accepted")
            .order_by(CallExecution.accepted_at, CallExecution.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if execution is None:
            return False
        try:
            validate_target(
                session, execution.request_payload, execution.kind == "emergency", dispatch=True
            )
        except DomainError as exc:
            if exc.code == "CALL_BUSY":
                return False
            local_failure(session, execution, "canceled", "ELIGIBILITY_CHANGED", at)
            return True
        execution.execution_status, execution.dispatched_at = "running", at
        execution.lease_token, execution.lease_until = token, at + timedelta(seconds=60)
        execution_id, payload = execution.id, execution.request_payload
    # No database transaction is held across the provider call.
    try:
        provider_call_id = provider.create(execution_id, payload)
    except Exception as exc:
        with Session(engine) as session, session.begin():
            row = session.scalar(
                select(CallExecution).where(CallExecution.id == execution_id).with_for_update()
            )
            if row.provider_call_id is None:
                if isinstance(exc, DispatchRejected):
                    local_failure(session, row, "failed", str(exc), at)
                    row.lease_until = None
                else:
                    row.execution_status, row.last_error = "unknown", "DISPATCH_OUTCOME_UNKNOWN"
        return True
    with Session(engine) as session, session.begin():
        row = session.scalar(
            select(CallExecution).where(CallExecution.id == execution_id).with_for_update()
        )
        if row.provider_call_id and row.provider_call_id != provider_call_id:
            row.execution_status, row.last_error = "unknown", "PROVIDER_ID_CONFLICT"
            return True
        row.provider_call_id = provider_call_id
        if row.call_id:
            call = session.get(Call, row.call_id)
            call.provider_call_id = provider_call_id
            call.started_at = call.started_at or at
        row.lease_until = None
    return True
