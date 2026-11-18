from typing import Annotated

from fastapi import APIRouter, Depends, Header
from sqlalchemy.orm import Session

from app.core.errors import API_ERRORS
from app.core.service_auth import require_service
from app.db.session import get_session
from app.modules.calls.schemas import StartCall, StartEmergency
from app.modules.calls.service import accept

router = APIRouter(tags=["calls"], responses=API_ERRORS)
Database = Annotated[Session, Depends(get_session)]
Caller = Annotated[str, Depends(require_service("calls:execute"))]
Key = Annotated[str, Header(min_length=1, max_length=200)]


@router.post("/internal/calls", status_code=202)
def start(data: StartCall, session: Database, caller: Caller, idempotency_key: Key):
    return accept(session, caller, data, idempotency_key)


@router.post("/internal/emergency-calls", status_code=202)
def emergency(data: StartEmergency, session: Database, caller: Caller, idempotency_key: Key):
    return accept(session, caller, data, idempotency_key, emergency=True)
