from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import DomainError
from app.db.models.call import Call, TranscriptTurn
from app.db.models.call_runtime import CallProcessingJob, OutboxMessage
from app.db.models.identity import Senior
from app.integrations.gemini.client import FakeModel
from app.modules.calls.transcripts import save_turns
from app.modules.memory.service import invalidate_search, normalize, search_memories
from app.modules.processing.service import finalize_ready, process_one


def ended(db_engine, call_data, empty=False):
    call_id = call_data["call_id"]
    if not empty:
        with Session(db_engine) as session:
            turn = {
                "turn_id": "turn-1",
                "speaker": "user",
                "text": "가상 공원 산책 이야기",
                "offset_ms": 100,
            }
            save_turns(session, call_id, [turn])
            save_turns(session, call_id, [turn])
            with pytest.raises(DomainError):
                save_turns(session, call_id, [{**turn, "text": "다른 내용"}])
    with Session(db_engine) as session, session.begin():
        row = session.get(Call, call_id)
        row.status = "completed"
        row.ended_at = datetime.now(UTC) - timedelta(minutes=1)
    finalize_ready(db_engine)


def test_summary_index_retry_without_resummarizing(db_engine, call_data):
    ended(db_engine, call_data)

    class Model(FakeModel):
        summaries = 0
        embeddings = 0

        def summarize(self, *args):
            self.summaries += 1
            return super().summarize(*args)

        def embed(self, *args):
            self.embeddings += 1
            if self.embeddings == 1:
                raise TimeoutError
            return super().embed(*args)

    model = Model()
    at = datetime.now(UTC)
    assert process_one(db_engine, model, get_settings(), at)
    with Session(db_engine) as session:
        call = session.get(Call, call_data["call_id"])
        assert call.summary_status == "completed" and call.indexing_status == "failed"
        assert call.embedding is None
    assert process_one(db_engine, model, get_settings(), at + timedelta(hours=1))
    assert model.summaries == 1 and model.embeddings == 2
    with Session(db_engine) as session:
        call = session.get(Call, call_data["call_id"])
        assert call.indexing_status == "completed" and len(call.embedding) == 768
        assert call.transcript_status == "partial"
        assert (
            session.scalar(
                select(func.count())
                .select_from(OutboxMessage)
                .where(OutboxMessage.operation == "call.interests")
            )
            == 1
        )
        assert session.scalar(select(func.count()).select_from(TranscriptTurn)) == 1


def test_empty_call_no_summary_or_job(db_engine, call_data):
    ended(db_engine, call_data, empty=True)
    assert not process_one(db_engine, FakeModel(), get_settings())
    with Session(db_engine) as session:
        call = session.get(Call, call_data["call_id"])
        assert call.summary_status == call.indexing_status == "not_applicable"
        assert call.transcript_status == "unavailable"


def test_consent_revoked_during_model_prevents_stale_save(db_engine, call_data):
    ended(db_engine, call_data)

    class Model(FakeModel):
        def summarize(self, *args):
            with Session(db_engine) as session, session.begin():
                session.get(Senior, call_data["senior_id"]).call_agree = False
            return super().summarize(*args)

    process_one(db_engine, Model(), get_settings())
    with Session(db_engine) as session:
        call = session.get(Call, call_data["call_id"])
        assert call.summary is None and call.embedding is None
        with pytest.raises(DomainError):
            session.rollback()
            search_memories(session, call_data["call_id"], "산책", FakeModel(), get_settings())


def test_revocation_invalidates_processing_and_retains_transcript(db_engine, call_data):
    ended(db_engine, call_data)
    process_one(db_engine, FakeModel(), get_settings())
    with Session(db_engine) as session:
        invalidate_search(session, call_data["senior_id"])
    with Session(db_engine) as session:
        call = session.get(Call, call_data["call_id"])
        assert call.summary and call.embedding is None
        assert session.get(CallProcessingJob, call.id).state == "canceled"
        assert session.scalar(select(func.count()).select_from(TranscriptTurn)) == 1


@pytest.mark.parametrize(
    "vector", [[0.0] * 768, [1.0] * 767, [float("nan")] * 768, [float("inf")] * 768]
)
def test_bad_embeddings_rejected(vector):
    with pytest.raises(ValueError):
        normalize(vector)
