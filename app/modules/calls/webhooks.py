import base64
import hmac
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl
from uuid import UUID
from xml.etree import ElementTree as ET

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.core.config import get_settings
from app.core.errors import DomainError
from app.db.models.call import AlertDelivery, EmergencyAlert
from app.db.session import get_session
from app.integrations.koreaaitel.client import DispatchRejected, domestic_number
from app.modules.calls.events import apply_event, bind_provider
from app.modules.calls.repository import TERMINAL, require_eligible, senior_for_call

router = APIRouter(tags=["koreaaitel"])


def stream_token(execution_id, call_id):
    key = get_settings().koreaaitel_signing_key.get_secret_value()
    if not key:
        raise ValueError("Stream signing key required")
    message = f"siniear-media-v1:{execution_id}:{call_id}".encode()
    return hmac.digest(key.encode(), message, "sha256").hex()


async def verified_form(request):
    settings = get_settings()
    key = settings.koreaaitel_signing_key.get_secret_value()
    if settings.call_provider == "disabled" or not key or not settings.public_base_url:
        raise DomainError(403, "FORBIDDEN", "전화 공급자가 비활성 상태입니다.")
    if request.headers.get("content-type", "").split(";")[0] != "application/x-www-form-urlencoded":
        raise DomainError(415, "VALIDATION_ERROR", "웹훅 본문 형식을 확인해 주세요.")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 65536:
            raise DomainError(413, "VALIDATION_ERROR", "요청 크기를 확인해 주세요.")
        body.extend(chunk)
    try:
        pairs = parse_qsl(
            body.decode(), keep_blank_values=True, max_num_fields=100, errors="strict"
        )
        form = dict(pairs)
        if len(form) != len(pairs):
            raise ValueError
    except ValueError:
        raise DomainError(422, "VALIDATION_ERROR", "웹훅 필드를 확인해 주세요.") from None
    url = settings.public_base_url.rstrip("/") + request.url.path + "?" + request.url.query
    signed = url + "".join(k + v for k, v in sorted(form.items()))
    expected = base64.b64encode(hmac.digest(key.encode(), signed.encode(), "sha256"))
    if not hmac.compare_digest(expected, request.headers.get("x-signature", "").encode()):
        raise DomainError(403, "FORBIDDEN", "공급자 서명이 일치하지 않습니다.")
    if (
        form.get("AccountId") != settings.koreaaitel_account_id
        or not form.get("CallId")
        or form.get("Direction") != "outbound"
    ):
        raise DomainError(403, "FORBIDDEN", "발신 계정과 통화 식별자를 확인해 주세요.")
    try:
        if domestic_number(form.get("From", "")) != domestic_number(
            settings.koreaaitel_from_number.get_secret_value()
        ):
            raise DispatchRejected
        form["To"] = domestic_number(form.get("To", ""))
    except DispatchRejected:
        raise DomainError(403, "FORBIDDEN", "발신·수신 번호를 확인해 주세요.") from None
    return form


@router.post("/webhooks/koreaaitel/status", status_code=204)
async def status(request: Request, execution_id: UUID, session: Session = Depends(get_session)):
    form = await verified_form(request)
    try:
        at = datetime.fromisoformat(form["Timestamp"])
        if at.utcoffset() is None or at > datetime.now(UTC) + timedelta(minutes=5):
            raise ValueError
        duration = int(form["Duration"]) if form.get("Duration") else None
        if duration is not None and not 0 <= duration <= 2**31 - 1:
            raise ValueError
    except (KeyError, ValueError, TypeError):
        raise DomainError(
            422, "VALIDATION_ERROR", "공급자 시각·통화 시간을 확인해 주세요."
        ) from None
    await run_in_threadpool(
        apply_event,
        session,
        execution_id,
        form["CallId"],
        form.get("CallStatus"),
        at.astimezone(UTC),
        duration,
        to=form["To"],
    )
    return Response(status_code=204)


def voice_xml(session, execution_id, form):
    with session.begin():
        row = bind_provider(session, execution_id, form["CallId"], to=form["To"])
        if row.provider_status in TERMINAL:
            raise DomainError(409, "CALL_INACTIVE", "종료된 통화입니다.")
        response = ET.Element("Response")
        if row.kind == "emergency":
            alert = session.get(
                EmergencyAlert, session.get(AlertDelivery, row.delivery_id).alert_id
            )
            require_eligible(session, senior_for_call(session, alert.call_id))
            ET.SubElement(response, "Say", language="ko-KR").text = row.request_payload["message"]
            ET.SubElement(response, "Hangup")
        else:
            require_eligible(session, senior_for_call(session, row.call_id))
            base = get_settings().public_base_url.rstrip("/").replace("https://", "wss://", 1)
            stream = ET.SubElement(
                ET.SubElement(response, "Connect"),
                "Stream",
                url=base + "/api/webhooks/koreaaitel/media",
                track="inbound",
            )
            ET.SubElement(stream, "Parameter", name="execution_id", value=str(row.id))
            ET.SubElement(
                stream, "Parameter", name="token", value=stream_token(row.id, form["CallId"])
            )
        return ET.tostring(response, encoding="unicode")


@router.post("/webhooks/koreaaitel/voice")
async def voice(request: Request, execution_id: UUID, session: Session = Depends(get_session)):
    form = await verified_form(request)
    xml = await run_in_threadpool(voice_xml, session, execution_id, form)
    return Response(
        content=xml, media_type="application/xml", headers={"Cache-Control": "no-store"}
    )
