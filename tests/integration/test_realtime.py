import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.websockets import WebSocketDisconnect

from app.db.models.call import TranscriptTurn
from app.db.models.call_runtime import CallExecution
from app.integrations.gemini import live as module
from app.integrations.koreaaitel.client import FakeProvider
from app.modules.calls.service import dispatch_one
from app.modules.realtime import router
from tests.integration.test_calls import accepted, voice_response


def test_stream_authentication_and_single_claim(api, db_engine, call_data, monkeypatch):
    accepted(db_engine, call_data)
    dispatch_one(db_engine, FakeProvider())
    with Session(db_engine) as session:
        row = session.scalar(select(CallExecution))
        eid, sid = row.id, row.provider_call_id
    stream = voice_response(api, eid, sid, "01000000001").find("Connect/Stream")
    path = "/api/webhooks/koreaaitel/media"
    assert stream.get("url") == "wss://calls.example.test" + path
    params = {p.get("name"): p.get("value") for p in stream.findall("Parameter")}
    assert params["execution_id"] == str(eid)
    start = {
        "customParameters": params,
        "callId": sid,
        "accountId": "ACsynthetic",
        "streamId": "MZsynthetic",
        "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
    }
    monkeypatch.setattr(router, "get_engine", lambda: db_engine)
    live = AsyncMock(return_value=True)
    monkeypatch.setattr(router, "live_session", live)

    def connect(fields):
        with api.websocket_connect(path) as ws:
            ws.send_json({"event": "connected", "protocol": "Call", "version": "1.0.0"})
            ws.send_json({"event": "start", "sequenceNumber": "1", "start": {**start, **fields}})
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_json()
            return closed.value.code

    assert connect({"customParameters": {"execution_id": str(eid)}}) == 1008
    assert connect({"customParameters": {**params, "token": "invalid"}}) == 1008
    assert connect({"accountId": "wrong"}) == 1008
    assert connect({"callId": "wrong"}) == 1008
    assert connect({}) == 1000
    assert connect({}) == 1008
    live.assert_awaited_once()
    assert live.await_args.args[2] == call_data["call_id"]


@pytest.mark.asyncio
async def test_live_fake_stream_audio_and_final_transcript(db_engine, call_data, monkeypatch):
    import asyncio

    end = asyncio.Event()

    class Live:
        async def send_client_content(self, **kw):
            pass

        async def send_realtime_input(self, **kw):
            if "audio" in kw:
                assert kw["audio"].mime_type == "audio/pcm;rate=16000"
                assert kw["audio"].data
            if kw.get("audio_stream_end"):
                end.set()

        async def receive(self):
            await end.wait()
            end.clear()
            yield SimpleNamespace(
                tool_call=None,
                server_content=SimpleNamespace(
                    interrupted=False,
                    model_turn=SimpleNamespace(
                        parts=[SimpleNamespace(inline_data=SimpleNamespace(data=bytes(960)))]
                    ),
                    input_transcription=SimpleNamespace(text="가상 인사"),
                    output_transcription=None,
                    turn_complete=True,
                ),
            )

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    client = SimpleNamespace(
        aio=SimpleNamespace(live=SimpleNamespace(connect=lambda **kw: Live()), aclose=AsyncMock())
    )
    monkeypatch.setattr(module.genai, "Client", lambda **kw: client)
    from app.core.config import get_settings

    settings = get_settings().model_copy(
        update={
            "call_provider": "koreaaitel",
            "allow_paid_calls": True,
            "gemini_live_model": "fake-live",
        }
    )
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    websocket = SimpleNamespace(
        receive_json=AsyncMock(
            side_effect=[
                {
                    "event": "media",
                    "sequenceNumber": "2",
                    "media": {"track": "inbound", "payload": base64.b64encode(bytes(160)).decode()},
                },
                {"event": "stop", "sequenceNumber": "3"},
            ]
        ),
        send_json=AsyncMock(),
    )
    assert await module.live_session(websocket, db_engine, call_data["call_id"])
    message = websocket.send_json.await_args.args[0]
    assert set(message) == {"event", "media"} and message["event"] == "media"
    assert len(base64.b64decode(message["media"]["payload"])) == 160
    with Session(db_engine) as session:
        assert session.scalar(select(TranscriptTurn)).text == "가상 인사"
