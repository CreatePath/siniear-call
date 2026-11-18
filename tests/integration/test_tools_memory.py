from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.idempotency import fingerprint
from app.db.models.call import Call, TranscriptTurn
from app.integrations.gemini.client import FakeModel
from app.modules.memory.service import normalize, search_memories
from app.modules.tools.service import ToolSession


def test_tool_confirmation_commits_before_c_and_timeout_uses_same_key(db_engine, call_data):
    mid = str(uuid4())
    requests = []

    class Client:
        def request(self, method, path, payload=None, key=None):
            if path.endswith("meeting-search"):
                return 200, {
                    "items": [
                        {"id": mid, "title": "가상 산책", "starts_at": "2026-11-30T15:30:00Z"}
                    ]
                }
            with Session(db_engine) as session:
                turn = session.get(
                    TranscriptTurn, (call_data["call_id"], payload["confirmation_turn_id"])
                )
                assert turn is not None and turn.action_context["meeting_id"] == mid
            requests.append((method, path, payload, key))
            if len(requests) == 1:
                raise TimeoutError
            return 201, {"id": str(uuid4()), "status": "confirmed"}

    tools = ToolSession(db_engine, call_data["call_id"], Client())
    assert tools.execute("confirm_pending", {})["error"] == "CONFIRMATION_REQUIRED"
    assert (
        tools.execute("search_meetings", {"senior_id": str(uuid4())})["error"]
        == "INVALID_TOOL_ARGUMENTS"
    )
    tools.execute("search_meetings", {})
    proposal = tools.execute("begin_registration", {"meeting_id": mid})
    assert proposal["read_exactly"] == (
        "가상 산책, 2026년 12월 1일 화요일 오전 12시 30분 일정에 참가 신청할까요?"
    )
    tools.record("assistant", proposal["read_exactly"], 100)
    tools.record("user", "네", 150)
    assert tools.execute("confirm_pending", {})["error"] == "CONFIRMATION_REQUIRED"
    tools.played(tools.announcement_turn_id)
    tools.record("user", "잘 모르겠어요", 200)
    assert tools.execute("confirm_pending", {})["error"] == "CONFIRMATION_REQUIRED"
    tools.record("assistant", proposal["read_exactly"], 250)
    tools.played(tools.announcement_turn_id)
    tools.record("user", "네", 300)
    assert tools.execute("confirm_pending", {})["error"] == "RESULT_UNKNOWN"
    assert tools.execute("begin_registration", {"meeting_id": mid})["error"] == "RESULT_UNKNOWN"
    tools.record("assistant", "결과를 확인하고 있습니다.", 400)
    assert tools.execute("confirm_pending", {})["confirmed_by_service"]
    assert requests[0] == requests[1]


def test_memory_hash_model_and_same_call_exclusion(db_engine, call_data):
    settings = get_settings()
    model = FakeModel()
    summary = "가상 공원 산책"
    with Session(db_engine) as session, session.begin():
        old = session.get(Call, call_data["call_id"])
        old.summary = summary
        old.summary_created_at = datetime.now(UTC)
        old.summary_status = old.indexing_status = "completed"
        old.embedding = normalize(model.embed(summary, "RETRIEVAL_DOCUMENT"))
        old.embedding_model_id = settings.embedding_model
        old.embedding_source_hash = fingerprint(summary)
        current = Call(id=uuid4(), episode_id=old.episode_id, attempt_no=2)
        session.add(current)
        current_id = current.id
    with Session(db_engine) as session:
        results = search_memories(session, current_id, summary, model, settings)
        assert results["items"][0]["call_id"] == str(call_data["call_id"])
        assert (
            search_memories(session, call_data["call_id"], summary, model, settings)["items"] == []
        )
    with Session(db_engine) as session, session.begin():
        session.get(Call, call_data["call_id"]).embedding_source_hash = "stale"
    with Session(db_engine) as session:
        assert search_memories(session, current_id, summary, model, settings)["items"] == []


def test_pending_action_changed_requires_new_announcement(db_engine, call_data):
    tools = ToolSession(db_engine, call_data["call_id"], None)
    one, two = str(uuid4()), str(uuid4())
    tools.meetings = {
        key: {"id": key, "title": "가상 일정", "starts_at": "2026-12-01T10:00:00+09:00"}
        for key in [one, two]
    }
    a = tools.execute("begin_registration", {"meeting_id": one})
    tools.record("assistant", a["read_exactly"], 100)
    tools.played(tools.announcement_turn_id)
    tools.record("user", "네", 200)
    tools.execute("begin_registration", {"meeting_id": two})
    assert tools.execute("confirm_pending", {})["error"] == "CONFIRMATION_REQUIRED"
