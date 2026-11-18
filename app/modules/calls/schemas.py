from typing import Annotated
from uuid import UUID

from pydantic import Field, StringConstraints

from app.core.schemas import Input, Interests, Name, Region

Phone = Annotated[str, StringConstraints(pattern=r"^\+[1-9][0-9]{7,14}$")]


class CallProfile(Input):
    name: Name
    region_code: Region
    interests: Interests


class StartCall(Input):
    call_id: UUID
    phone_number: Phone
    profile: CallProfile


class StartEmergency(Input):
    alert_id: UUID
    delivery_id: UUID
    contact_id: UUID
    phone_number: Phone
    message: str = Field(min_length=1, max_length=2000)
