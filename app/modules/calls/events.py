from datetime import UTC, datetime

from sqlalchemy import select

from app.core.errors import DomainError
from app.core.idempotency import fingerprint
from app.db.models.call import Call
from app.db.models.call_runtime import CallExecution, InboxEvent
from app.integrations.koreaaitel.client import domestic_number
from app.modules.calls.repository import TERMINAL
from app.modules.calls.service import emit_state

RANK = {"queued": 0, "initiated": 1, "ringing": 2, "in_progress": 3}
PROVIDER_STATES = {
    "queued": "queued",
    "initiated": "initiated",
    "ringing": "ringing",
    "in-progress": "in_progress",
    "completed": "completed",
    "busy": "busy",
    "no-answer": "no_answer",
    "failed": "failed",
    "canceled": "canceled",
    "rejected": "canceled",
}


def bind_provider(session, execution_id, provider_call_id, *, to=None):
    row = session.scalar(
        select(CallExecution).where(CallExecution.id == execution_id).with_for_update()
    )
    if row is None or row.dispatched_at is None:
        raise DomainError(404, "CALL_NOT_FOUND", "발신 실행을 찾을 수 없습니다.")
    if row.provider_call_id not in {None, provider_call_id}:
        raise DomainError(409, "EXECUTION_MISMATCH", "통화 식별자가 일치하지 않습니다.")
    if to is not None and to != domestic_number(row.request_payload["phone_number"]):
        raise DomainError(403, "FORBIDDEN", "수신 대상이 다릅니다.")
    row.provider_call_id = provider_call_id
    if row.call_id:
        session.get(Call, row.call_id).provider_call_id = provider_call_id
    return row


def apply_event(
    session, execution_id, provider_call_id, status, occurred_at, duration=None, *, to=None
):
    if status not in PROVIDER_STATES:
        raise DomainError(422, "VALIDATION_ERROR", "통화 이벤트 형식을 확인해 주세요.")
    state = PROVIDER_STATES[status]
    # Korea AI Telecom sends a timestamp but no sequence. One transition per phase.
    sequence = RANK.get(state, 4)
    with session.begin():
        row = bind_provider(session, execution_id, provider_call_id, to=to)
        event_id = f"koreaaitel:{provider_call_id}:{status}"
        payload = {
            "provider_call_id": provider_call_id,
            "status": status,
            "sequence": sequence,
            "at": occurred_at.isoformat(),
            "duration": duration,
        }
        cached = session.scalar(
            select(InboxEvent).where(
                InboxEvent.consumer_service == "T", InboxEvent.event_id == event_id
            )
        )
        if cached:
            if cached.payload_hash != fingerprint(payload):
                raise DomainError(409, "EVENT_CONFLICT", "같은 이벤트의 내용이 다릅니다.")
            return
        session.add(
            InboxEvent(
                consumer_service="T",
                event_id=event_id,
                resource_id=row.call_id or row.delivery_id,
                payload_hash=fingerprint(payload),
                payload=payload,
            )
        )
        call = session.get(Call, row.call_id) if row.call_id else None
        if sequence <= row.provider_sequence or row.provider_status in TERMINAL:
            return
        if state == "no_answer" and (
            duration
            or row.provider_status == "in_progress"
            or row.stream_id
            or (call and call.answered_at)
        ):
            # A contradictory callback cannot turn an answered call into an emergency trigger.
            row.last_error = "CONTRADICTORY_PROVIDER_STATE"
            return
        row.provider_status, row.provider_sequence = state, sequence
        if row.execution_status == "unknown":
            row.execution_status = "running"
        if state in TERMINAL:
            row.execution_status = "completed"
            row.lease_until = None
        if call:
            call.status = state
            call.started_at = call.started_at or row.dispatched_at
            if state == "in_progress":
                call.answered_at = call.answered_at or occurred_at
            if state in TERMINAL:
                call.ended_at = max(occurred_at, call.started_at)
                if (
                    state == "completed"
                    and duration is not None
                    and duration > 0
                    and call.answered_at is None
                ):
                    from datetime import timedelta

                    call.answered_at = max(
                        call.started_at, call.ended_at - timedelta(seconds=duration)
                    )
        emit_state(session, row, state, occurred_at, sequence)


def close_media(session, call_id, complete):
    with session.begin():
        execution = session.scalar(
            select(CallExecution).where(CallExecution.call_id == call_id).with_for_update()
        )
        execution.media_closed_at = datetime.now(UTC)
        call = session.get(Call, call_id)
        call.transcript_status = "complete" if complete else "partial"
