import base64
import hmac
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from xml.etree import ElementTree as ET

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import DomainError
from app.db.models.call import Call
from app.db.models.call_runtime import CallExecution, InboxEvent, OutboxMessage
from app.db.models.identity import Senior
from app.integrations.koreaaitel.client import FakeProvider, KoreaAITelProvider
from app.modules.calls.events import apply_event
from app.modules.calls.schemas import StartCall
from app.modules.calls.service import accept, dispatch_one


def accepted(db_engine, call_data):
    with Session(db_engine) as session:
        accept(session, "C", StartCall(**call_data["payload"]), "key-1")


def test_auth_idempotency_and_execution_identity(api, db_engine, call_data):
    payload = call_data["payload"]
    path = "/api/internal/calls"
    headers = {"Authorization": "Bearer test-C-to-T", "Idempotency-Key": "key-1"}
    assert (
        api.post(
            path,
            json=payload,
            headers={"Authorization": "Bearer dev:fake", "Idempotency-Key": "key"},
        ).status_code
        == 401
    )
    assert api.post(path, json=payload, headers=headers).status_code == 202
    assert api.post(path, json=payload, headers=headers).status_code == 202
    assert (
        api.post(path, json=payload, headers={**headers, "Idempotency-Key": "key-2"}).status_code
        == 202
    )
    assert (
        api.post(
            path, json={**payload, "phone_number": "+821000000002"}, headers=headers
        ).status_code
        == 409
    )
    with Session(db_engine) as session:
        assert session.scalar(select(func.count()).select_from(CallExecution)) == 1


def test_concurrent_dispatch_and_unknown_never_redials(api, db_engine, call_data):
    accepted(db_engine, call_data)

    class LostResponse:
        count = 0

        def create(self, *args):
            self.count += 1
            raise TimeoutError

    provider = LostResponse()
    at = datetime.now(UTC)
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda _: dispatch_one(db_engine, provider, at), range(2)))
    assert provider.count == 1
    assert not dispatch_one(db_engine, provider, at + timedelta(minutes=10))
    with Session(db_engine) as session:
        row = session.scalar(select(CallExecution))
        assert row.execution_status == "unknown"
        path = f"/api/webhooks/koreaaitel/status?execution_id={row.id}"
    form = dict(
        outbound_form("CAlost", "01000000001"), CallStatus="ringing", Timestamp=at.isoformat()
    )
    assert api.post(path, data=form, headers=signed_headers(path, form)).status_code == 204
    with Session(db_engine) as session:
        row = session.scalar(select(CallExecution))
        assert (row.execution_status, row.provider_call_id) == ("running", "CAlost")
    assert provider.count == 1


def test_revocation_before_dispatch_cancels_without_provider(db_engine, call_data):
    accepted(db_engine, call_data)
    with Session(db_engine) as session, session.begin():
        session.get(Senior, call_data["senior_id"]).call_agree = False

    class Never:
        def create(self, *args):
            pytest.fail("must not dial")

    dispatch_one(db_engine, Never())
    with Session(db_engine) as session:
        assert session.get(Call, call_data["call_id"]).status == "canceled"


@pytest.mark.parametrize(("code", "expected"), [(429, "failed"), (503, "unknown")])
def test_provider_error_never_redials(db_engine, call_data, monkeypatch, code, expected):
    accepted(db_engine, call_data)
    requests = []

    def post(url, **kwargs):
        requests.append(url)
        return httpx.Response(code, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", post)
    provider = KoreaAITelProvider(get_settings())
    assert dispatch_one(db_engine, provider)
    assert not dispatch_one(db_engine, provider)
    assert len(requests) == 1
    with Session(db_engine) as session:
        row = session.scalar(select(CallExecution))
        assert row.execution_status == expected
        events = session.scalars(
            select(OutboxMessage).where(OutboxMessage.operation == "call.event")
        )
        assert [event.payload["status"] for event in events] == (["failed"] if code == 429 else [])


def test_status_order_deduplication_and_atomic_event(db_engine, call_data):
    accepted(db_engine, call_data)
    dispatch_one(db_engine, FakeProvider())
    at = datetime.now(UTC)
    with Session(db_engine) as session:
        row = session.scalar(select(CallExecution))
        eid, sid = row.id, row.provider_call_id

    def apply(status, when=at):
        with Session(db_engine) as session:
            apply_event(session, eid, sid, status, when)

    apply("in-progress")
    apply("ringing")
    apply("completed")
    apply("completed")
    apply("initiated")
    with Session(db_engine) as session:
        call = session.get(Call, call_data["call_id"])
        assert call.status == "completed" and call.answered_at is not None
        assert (
            session.scalar(
                select(func.count())
                .select_from(OutboxMessage)
                .where(OutboxMessage.producer_service == "T")
            )
            == 2
        )
    with pytest.raises(DomainError):
        apply("completed", at + timedelta(seconds=1))


def signed_headers(path, form):
    data = "https://calls.example.test" + path + "".join(k + v for k, v in sorted(form.items()))
    signature = base64.b64encode(hmac.digest(b"test-provider-secret", data.encode(), "sha256"))
    return {"X-Signature": signature.decode()}


def outbound_form(sid, number):
    return {
        "AccountId": "ACsynthetic",
        "CallId": sid,
        "Direction": "outbound",
        "From": "07000000000",
        "To": number,
    }


@pytest.mark.parametrize(
    ("status", "expected"),
    [("no-answer", "no_answer"), ("rejected", "canceled"), ("busy", "busy"), ("failed", "failed")],
)
def test_signed_callbacks(api, db_engine, call_data, status, expected):
    accepted(db_engine, call_data)
    dispatch_one(db_engine, FakeProvider())
    with Session(db_engine) as session:
        row = session.scalar(select(CallExecution))
        eid, sid = row.id, row.provider_call_id
    path = f"/api/webhooks/koreaaitel/status?execution_id={eid}"
    form = dict(
        outbound_form(sid, "01000000001"),
        CallStatus=status,
        Timestamp=datetime.now(UTC).isoformat(),
        Duration="0",
    )
    headers = signed_headers(path, form)
    assert api.post(path, data=form).status_code == 403
    assert (
        api.post(path, data={**form, "CallStatus": "completed"}, headers=headers).status_code == 403
    )
    for field, wrong in [("To", "01000000002"), ("AccountId", "other"), ("Direction", "inbound")]:
        invalid = {**form, field: wrong}
        assert (
            api.post(path, data=invalid, headers=signed_headers(path, invalid)).status_code == 403
        )
    for _ in range(2):
        assert api.post(path, data=form, headers=headers).status_code == 204
    with Session(db_engine) as session:
        assert session.get(Call, call_data["call_id"]).status == expected
        assert session.scalar(select(func.count()).select_from(InboxEvent)) == 1


def voice_response(api, eid, sid, number):
    path = f"/api/webhooks/koreaaitel/voice?execution_id={eid}"
    form = outbound_form(sid, number)
    assert api.post(path, data=form).status_code == 403
    response = api.post(path, data=form, headers=signed_headers(path, form))
    assert response.status_code == 200
    return ET.fromstring(response.text)
