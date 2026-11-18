import os
from datetime import UTC, datetime, time
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models.call import Call, CallEpisode, CallSchedule
from app.db.models.call_runtime import OutboxMessage
from app.db.models.identity import IdentityVerification, Senior, User
from app.db.session import database_url, get_session
from app.main import app


@pytest.fixture
def db_engine(monkeypatch):
    if os.environ.get("RUN_DB_TESTS") != "1":
        pytest.skip("RUN_DB_TESTS=1 enables C migration/PostgreSQL contract tests")
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("CALL_PROVIDER", "fake")
    monkeypatch.setenv("INTERNAL_INBOUND_TOKEN", "test-C-to-T")
    monkeypatch.setenv("KOREAAITEL_SIGNING_KEY", "test-provider-secret")
    monkeypatch.setenv("KOREAAITEL_FROM_NUMBER", "07000000000")
    monkeypatch.setenv("KOREAAITEL_ACCOUNT_ID", "ACsynthetic")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://calls.example.test")
    get_settings.cache_clear()
    schema = "test_call_" + uuid4().hex
    admin = create_engine(database_url(), hide_parameters=True)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(
        database_url(),
        hide_parameters=True,
        connect_args={"options": f"-csearch_path={schema} -clock_timeout=5000"},
    )
    migration_root = Path(os.environ.get("SERVICE_SCHEMA_PATH", "/schema"))
    if not migration_root.exists():
        migration_root = Path("../siniear-service")
    try:
        with engine.begin() as connection:
            config = Config(str(migration_root / "alembic.ini"))
            config.set_main_option("script_location", str(migration_root / "migrations"))
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
        get_settings.cache_clear()


@pytest.fixture
def call_data(db_engine):
    with Session(db_engine) as session, session.begin():
        user = User(id=uuid4())
        session.add(user)
        session.flush()
        senior = Senior(
            id=uuid4(),
            user_id=user.id,
            name="가상어르신",
            phone_number="+821000000001",
            region_code="TEST",
            interests=["산책"],
            call_agree=True,
        )
        session.add(senior)
        session.add(
            IdentityVerification(user_id=user.id, status="verified", verified_at=datetime.now(UTC))
        )
        session.flush()
        schedule = CallSchedule(
            id=uuid4(), senior_id=senior.id, days_of_week=["MON"], local_time=time(10), enabled=True
        )
        session.add(schedule)
        session.flush()
        episode = CallEpisode(id=uuid4(), schedule_id=schedule.id, scheduled_at=datetime.now(UTC))
        session.add(episode)
        session.flush()
        call = Call(id=uuid4(), episode_id=episode.id, attempt_no=1)
        session.add(call)
        session.flush()
        payload = {
            "call_id": str(call.id),
            "phone_number": senior.phone_number,
            "profile": {
                "name": senior.name,
                "region_code": senior.region_code,
                "interests": senior.interests,
            },
        }
        session.add(
            OutboxMessage(
                producer_service="C",
                operation="call.start",
                resource_id=call.id,
                dedupe_key=f"call:{call.id}",
                available_at=datetime.now(UTC),
                payload=payload,
            )
        )
        return {
            "call_id": call.id,
            "senior_id": senior.id,
            "schedule_id": schedule.id,
            "payload": payload,
        }


@pytest.fixture
def api(db_engine):
    def override():
        with Session(db_engine, expire_on_commit=False) as session:
            yield session

    app.dependency_overrides[get_session] = override
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_session, None)
