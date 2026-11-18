"""Run the sibling C app in a separate process against the same isolated schema."""

import os
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.request import urlopen

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models.call import AlertDelivery, Call, CareContact, EmergencyAlert
from app.db.models.call_runtime import CallExecution, OutboxMessage
from app.integrations.koreaaitel.client import FakeProvider
from app.integrations.service_server import send
from app.modules.calls.events import apply_event
from app.modules.calls.service import dispatch_one
from app.workers.outbox import deliver_one
from tests.integration.test_calls import voice_response

RUNNER = """
import sys
sys.path.insert(0, sys.argv[1])
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.db.session import database_url, get_session
engine=create_engine(database_url(),hide_parameters=True,connect_args={"options":"-csearch_path="+sys.argv[2]})
if sys.argv[3]=='tick':
    from datetime import datetime
    from app.modules.calls.scheduler import tick
    print(tick(engine,datetime.fromisoformat(sys.argv[4])))
else:
    import uvicorn
    from app.main import app
    def session_override():
        with Session(engine,expire_on_commit=False) as session:yield session
    app.dependency_overrides[get_session]=session_override
    uvicorn.run(app,host='127.0.0.1',port=int(sys.argv[3]),log_level='critical',access_log=False)
"""


@pytest.fixture
def c_service(db_engine, monkeypatch):
    root = Path(os.environ.get("SERVICE_SOURCE_PATH", "/service"))
    if not (root / "app").exists():
        root = Path("../siniear-service").resolve()
    assert (root / "app/main.py").exists(), "C sibling checkout is required for contract tests"
    with db_engine.connect() as conn:
        schema = conn.scalar(text("select current_schema()"))
    env = {
        **os.environ,
        "APP_ENV": "test",
        "DEV_AUTH_ENABLED": "true",
        "INTERNAL_INBOUND_TOKEN": "test-T-to-C",
        "INTERNAL_SCOPES": "calls:events meetings:read meetings:write interests:write alerts:read",
    }
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    args = [sys.executable, "-c", RUNNER, str(root), schema]
    process = subprocess.Popen(
        [*args, str(port)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    try:
        for _ in range(100):
            if process.poll() is not None:
                pytest.fail(process.stderr.read().decode())
            try:
                with urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            pytest.fail("C test server did not start")
        monkeypatch.setenv("PEER_BASE_URL", f"http://127.0.0.1:{port}")
        monkeypatch.setenv("INTERNAL_OUTBOUND_TOKEN", "test-T-to-C")
        get_settings.cache_clear()

        def tick(at):
            result = subprocess.run(
                [*args, "tick", at.isoformat()], env=env, capture_output=True, text=True, timeout=15
            )
            assert result.returncode == 0, result.stderr
            return int(result.stdout.strip())

        yield tick
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        process.stderr.close()


def test_three_checkins_then_emergency_over_real_c_http(api, db_engine, call_data, c_service):
    at = datetime.now(UTC)
    with Session(db_engine) as session, session.begin():
        session.add(
            CareContact(
                senior_id=call_data["senior_id"],
                name="가상 연락처",
                phone_number="+821000000009",
                created_at=at,
            )
        )

    def start_pending():
        with Session(db_engine) as session:
            messages = list(
                session.scalars(select(OutboxMessage).where(OutboxMessage.producer_service == "C"))
            )
            payloads = [(m.operation, m.payload, m.dedupe_key) for m in messages]
        for operation, payload, key in payloads:
            path = (
                "/api/internal/calls"
                if operation == "call.start"
                else "/api/internal/emergency-calls"
            )
            response = api.post(
                path,
                json=payload,
                headers={"Authorization": "Bearer test-C-to-T", "Idempotency-Key": key},
            )
            assert response.status_code == 202, response.text
        dispatch_one(db_engine, FakeProvider())

    def result(status, at):
        with Session(db_engine) as session:
            execution = session.scalar(
                select(CallExecution).where(CallExecution.execution_status == "running")
            )
            eid, sid = execution.id, execution.provider_call_id
        with Session(db_engine) as session:
            apply_event(session, eid, sid, "ringing", at - timedelta(seconds=1))
        with Session(db_engine) as session:
            apply_event(session, eid, sid, status, at)
        while deliver_one(db_engine, "T", send, at + timedelta(seconds=1)):
            pass

    for attempt in range(1, 4):
        start_pending()
        at += timedelta(seconds=1)
        result("no-answer", at)
        if attempt < 3:
            assert c_service(at + timedelta(seconds=179)) == 0
            assert c_service(at + timedelta(minutes=3)) == 1
            at += timedelta(minutes=3)
    with Session(db_engine) as session:
        assert session.scalar(select(func.count()).select_from(Call)) == 3
        alert = session.scalar(select(EmergencyAlert))
        assert alert.status == "contacting"
        assert session.scalar(select(func.count()).select_from(AlertDelivery)) == 1
    assert c_service(at + timedelta(minutes=10)) == 0
    start_pending()
    with Session(db_engine) as session:
        row = session.scalar(select(CallExecution).where(CallExecution.kind == "emergency"))
        xml = voice_response(api, row.id, row.provider_call_id, "01000000009")
        assert xml.find("Say").text == row.request_payload["message"]
        assert xml.find("Hangup") is not None and xml.find("Connect") is None
    result("completed", at + timedelta(seconds=30))
    with Session(db_engine) as session:
        assert session.scalar(select(EmergencyAlert)).status == "completed"
        assert session.scalar(select(AlertDelivery)).status == "completed"
        assert session.scalar(select(AlertDelivery)).ended_at == at + timedelta(seconds=30)
