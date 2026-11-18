from sqlalchemy import select

from app.core.errors import DomainError
from app.db.models.call import Call, TranscriptTurn
from app.db.models.call_runtime import CallProcessingJob
from app.modules.calls.repository import require_eligible, senior_for_call


def save_turns(session, call_id, turns):
    with session.begin():
        require_eligible(session, senior_for_call(session, call_id, lock=True))
        call = session.scalar(select(Call).where(Call.id == call_id).with_for_update())
        for turn in turns:
            if (
                turn.get("speaker") not in {"user", "assistant"}
                or not turn.get("text", "").strip()
                or not turn.get("turn_id")
                or len(turn["turn_id"]) > 200
                or len(turn["text"]) > 20000
                or turn.get("offset_ms", -1) < 0
            ):
                raise DomainError(422, "VALIDATION_ERROR", "발화 형식을 확인해 주세요.")
            values = {k: turn.get(k) for k in ("speaker", "text", "offset_ms", "action_context")}
            existing = session.get(TranscriptTurn, (call_id, turn["turn_id"]))
            if existing:
                if any(getattr(existing, k) != v for k, v in values.items()):
                    raise DomainError(409, "TURN_CONFLICT", "같은 발화 ID의 내용이 다릅니다.")
                continue
            if call.summary_status != "pending" or session.get(CallProcessingJob, call_id):
                raise DomainError(409, "TRANSCRIPT_FINALIZED", "이미 확정된 원문입니다.")
            session.add(TranscriptTurn(call_id=call_id, turn_id=turn["turn_id"], **values))
            session.flush()
