import asyncio
import hmac
import json
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import DomainError
from app.db.models.call_runtime import CallExecution
from app.db.session import get_engine
from app.integrations.gemini.live import live_session
from app.modules.calls.events import close_media
from app.modules.calls.repository import TERMINAL, require_eligible, senior_for_call
from app.modules.calls.webhooks import stream_token

router = APIRouter()


def claim_stream(engine, start):
    params = start["customParameters"]
    execution_id = UUID(params["execution_id"])
    if not hmac.compare_digest(
        stream_token(execution_id, start["callId"]).encode(), params["token"].encode()
    ):
        raise ValueError("Invalid stream token")
    with Session(engine) as session, session.begin():
        row = session.scalar(
            select(CallExecution).where(CallExecution.id == execution_id).with_for_update()
        )
        if (
            row is None
            or row.kind != "check_in"
            or row.provider_call_id != start["callId"]
            or row.provider_status in TERMINAL
            or row.stream_id is not None
            or not isinstance(start["streamId"], str)
            or not start["streamId"]
            or start["accountId"] != get_settings().koreaaitel_account_id
            or start["mediaFormat"]
            != {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1}
        ):
            raise ValueError("Invalid stream")
        require_eligible(session, senior_for_call(session, row.call_id))
        row.stream_id = start["streamId"]
        return row.call_id


@router.websocket("/webhooks/koreaaitel/media")
async def media(websocket: WebSocket):
    if get_settings().call_provider == "disabled":
        await websocket.close(code=1008)
        return
    await websocket.accept()
    engine = get_engine()
    call_id = None
    complete = False
    try:
        connected = await asyncio.wait_for(websocket.receive_json(), 10)
        if (
            connected.get("event") != "connected"
            or connected.get("protocol") != "Call"
            or connected.get("version") != "1.0.0"
        ):
            raise ValueError("Invalid protocol")
        message = await asyncio.wait_for(websocket.receive_json(), 10)
        if message.get("event") != "start":
            raise ValueError("Start required")
        call_id = await asyncio.to_thread(claim_stream, engine, message["start"])
        complete = await live_session(websocket, engine, call_id)
    except (
        ValueError,
        KeyError,
        TypeError,
        DomainError,
        TimeoutError,
        WebSocketDisconnect,
        json.JSONDecodeError,
    ):
        pass
    except Exception:
        # Provider exceptions may contain credentials or transcript content; never log them.
        pass
    finally:
        if call_id:

            def finish():
                with Session(engine) as session:
                    close_media(session, call_id, complete)

            await asyncio.to_thread(finish)
        try:
            await websocket.close(code=1008 if call_id is None else (1000 if complete else 1011))
        except RuntimeError:
            pass
