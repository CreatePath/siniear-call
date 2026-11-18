import math

from sqlalchemy import select

from app.core.idempotency import fingerprint
from app.db.models.call import Call, CallEpisode
from app.modules.calls.repository import call_query, require_eligible, senior_for_call


def normalize(values, dimensions=768):
    if len(values) != dimensions or any(not math.isfinite(x) for x in values):
        raise ValueError("Invalid embedding dimensions or values")
    norm = math.sqrt(sum(x * x for x in values))
    if not math.isfinite(norm) or norm == 0:
        raise ValueError("Invalid embedding norm")
    return [x / norm for x in values]


def prepare_context(session, call_id):
    with session.begin():
        senior = senior_for_call(session, call_id)
        require_eligible(session, senior)
        calls = session.scalars(
            call_query(senior.id)
            .where(
                Call.id != call_id, Call.summary_status == "completed", Call.summary.is_not(None)
            )
            .order_by(CallEpisode.scheduled_at.desc(), Call.attempt_no.desc())
            .limit(3)
        )
        return {
            "profile": {
                "name": senior.name,
                "region_code": senior.region_code,
                "interests": senior.interests,
            },
            "recent_summaries": [
                {
                    "call_id": str(c.id),
                    "summary": c.summary,
                    "created_at": c.summary_created_at.isoformat(),
                }
                for c in calls
            ],
        }


def search_memories(session, call_id, query, model, settings, top_k=3):
    if not 1 <= top_k <= 10 or not query.strip() or len(query) > 2000:
        raise ValueError("Invalid search request")
    with session.begin():
        require_eligible(session, senior_for_call(session, call_id))
    vector = normalize(model.embed(query, "RETRIEVAL_QUERY"), settings.embedding_dimensions)
    with session.begin():
        senior = senior_for_call(session, call_id)
        require_eligible(session, senior)
        rows = session.scalars(
            call_query(senior.id)
            .where(
                Call.id != call_id,
                Call.summary_status == "completed",
                Call.indexing_status == "completed",
                Call.embedding_model_id == settings.embedding_model,
                Call.summary.is_not(None),
                Call.embedding.is_not(None),
            )
            .order_by(CallEpisode.scheduled_at.desc())
            .limit(200)
        )
        results = []
        for row in rows:
            if row.embedding_source_hash != fingerprint(row.summary):
                continue
            try:
                stored = normalize(row.embedding, settings.embedding_dimensions)
            except (TypeError, ValueError):
                continue
            score = max(-1, min(1, sum(x * y for x, y in zip(vector, stored, strict=True))))
            if score >= settings.memory_min_score:
                results.append(
                    {"call_id": str(row.id), "excerpt": row.summary[:1000], "score": score}
                )
        return {"items": sorted(results, key=lambda x: (-x["score"], x["call_id"]))[:top_k]}


def invalidate_search(session, senior_id):
    """P1 동의 철회 트랜잭션과 연결할 T 소유 작업. 원문·요약은 보존한다."""
    from app.db.models.call_runtime import CallProcessingJob, OutboxMessage

    with session.begin():
        for call in session.scalars(call_query(senior_id).with_for_update(of=Call)):
            call.embedding = call.embedding_model_id = call.embedding_source_hash = None
            call.indexing_status = "not_applicable"
            if call.summary is None:
                call.summary_status = "not_applicable"
            job = session.get(CallProcessingJob, call.id)
            if job:
                job.state, job.lease_token, job.lease_until = "canceled", None, None
            for message in session.scalars(
                select(OutboxMessage).where(
                    OutboxMessage.producer_service == "T",
                    OutboxMessage.operation == "call.interests",
                    OutboxMessage.resource_id == call.id,
                    OutboxMessage.state.in_(["pending", "sending"]),
                )
            ):
                message.state = "canceled"
