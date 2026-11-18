from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.idempotency import fingerprint
from app.db.models.call import Call, TranscriptTurn
from app.db.models.call_runtime import CallExecution, CallProcessingJob, OutboxMessage
from app.modules.calls.repository import TERMINAL, require_eligible, senior_for_call
from app.modules.memory.service import normalize


def transcript(session, call_id):
    return [
        {"turn_id": t.turn_id, "speaker": t.speaker, "text": t.text, "offset_ms": t.offset_ms}
        for t in session.scalars(
            select(TranscriptTurn)
            .where(TranscriptTurn.call_id == call_id)
            .order_by(TranscriptTurn.offset_ms, TranscriptTurn.turn_id)
        )
    ]


def finalize_ready(engine, at=None):
    at = at or datetime.now(UTC)
    with Session(engine) as session, session.begin():
        rows = session.scalars(
            select(Call)
            .where(
                Call.status.in_(TERMINAL),
                Call.summary_status == "pending",
                Call.ended_at.is_not(None),
                ~select(CallProcessingJob.call_id)
                .where(CallProcessingJob.call_id == Call.id)
                .exists(),
            )
            .with_for_update(skip_locked=True)
        ).all()
        for call in rows:
            execution = session.scalar(
                select(CallExecution).where(CallExecution.call_id == call.id)
            )
            if (
                execution
                and execution.stream_id
                and not execution.media_closed_at
                and call.ended_at + timedelta(seconds=30) > at
            ):
                continue
            if not execution or not execution.media_closed_at:
                if call.ended_at + timedelta(seconds=30) > at and call.answered_at:
                    continue
                call.transcript_status = "partial"
            turns = transcript(session, call.id)
            if not turns:
                call.transcript_status = "unavailable"
                call.summary_status = call.indexing_status = "not_applicable"
                continue
            session.add(
                CallProcessingJob(call_id=call.id, input_hash=fingerprint(turns), available_at=at)
            )


def process_one(engine, model, settings, at=None):
    at = at or datetime.now(UTC)
    token = uuid4()
    with Session(engine) as session, session.begin():
        job = session.scalar(
            select(CallProcessingJob)
            .where(
                CallProcessingJob.state.in_(["pending", "processing", "failed"]),
                CallProcessingJob.available_at <= at,
                or_(CallProcessingJob.lease_until.is_(None), CallProcessingJob.lease_until < at),
            )
            .order_by(CallProcessingJob.available_at, CallProcessingJob.call_id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if job is None:
            return False
        senior = senior_for_call(session, job.call_id)
        if not senior.call_agree:
            job.state = "canceled"
            return True
        call = session.get(Call, job.call_id)
        turns = transcript(session, call.id)
        if fingerprint(turns) != job.input_hash:
            job.state, job.last_error = "canceled", "INPUT_CHANGED"
            return True
        job.state, job.lease_token, job.lease_until = "processing", token, at + timedelta(minutes=5)
        job.attempts += 1
        call_id, input_hash, summary = call.id, job.input_hash, call.summary
        partial = call.transcript_status != "complete"
        if summary is None:
            call.summary_status = "processing"
        else:
            call.indexing_status = "processing"
    stage = "summary" if summary is None else "index"
    try:
        if summary is None:
            result = model.summarize(turns, partial)
            summary = result["summary"].strip()
            if not summary or len(summary) > 10000:
                raise ValueError("Invalid summary")
            interests = list(
                dict.fromkeys(
                    s.strip()
                    for s in result.get("interests", [])
                    if isinstance(s, str) and 0 < len(s.strip()) <= 50
                )
            )[:20]
            with Session(engine) as session, session.begin():
                require_eligible(session, senior_for_call(session, call_id, lock=True))
                job = session.scalar(
                    select(CallProcessingJob)
                    .where(CallProcessingJob.call_id == call_id)
                    .with_for_update()
                )
                if (
                    job.lease_token != token
                    or job.state != "processing"
                    or fingerprint(transcript(session, call_id)) != input_hash
                ):
                    return True
                call = session.get(Call, call_id)
                call.summary, call.summary_created_at, call.summary_status = (
                    summary,
                    at,
                    "completed",
                )
                call.embedding = call.embedding_model_id = call.embedding_source_hash = None
                call.indexing_status = "processing"
                session.add(
                    OutboxMessage(
                        producer_service="T",
                        operation="call.interests",
                        resource_id=call_id,
                        dedupe_key=f"interests:{call_id}:{input_hash}",
                        available_at=at,
                        payload={"interests": interests},
                    )
                )
        stage = "index"
        vector = normalize(
            model.embed(summary, "RETRIEVAL_DOCUMENT"), settings.embedding_dimensions
        )
        with Session(engine) as session, session.begin():
            require_eligible(session, senior_for_call(session, call_id, lock=True))
            job = session.scalar(
                select(CallProcessingJob)
                .where(CallProcessingJob.call_id == call_id)
                .with_for_update()
            )
            call = session.scalar(select(Call).where(Call.id == call_id).with_for_update())
            if (
                job.lease_token != token
                or job.state != "processing"
                or call.summary != summary
                or fingerprint(transcript(session, call_id)) != input_hash
            ):
                return True
            call.embedding, call.embedding_model_id, call.embedding_source_hash = (
                vector,
                settings.embedding_model,
                fingerprint(summary),
            )
            call.indexing_status, job.state = "completed", "completed"
            job.lease_token = job.lease_until = job.last_error = None
    except Exception:
        with Session(engine) as session, session.begin():
            job = session.scalar(
                select(CallProcessingJob)
                .where(CallProcessingJob.call_id == call_id)
                .with_for_update()
            )
            if job and job.lease_token == token and job.state == "processing":
                job.state, job.last_error = "failed", f"{stage.upper()}_FAILED"
                job.available_at = at + timedelta(seconds=min(3600, 30 * 2 ** min(job.attempts, 6)))
                job.lease_token = job.lease_until = None
                call = session.get(Call, call_id)
                if stage == "summary":
                    call.summary_status = "failed"
                else:
                    call.indexing_status = "failed"
    return True
