from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (UniqueConstraint("owner_service", "subject_id", "operation", "key"),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_service: Mapped[str] = mapped_column(Text, default="C")
    subject_id: Mapped[str] = mapped_column(Text)
    operation: Mapped[str] = mapped_column(Text)
    key: Mapped[str] = mapped_column(Text)
    request_hash: Mapped[str] = mapped_column(Text)
    http_status: Mapped[int]
    response_body: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
