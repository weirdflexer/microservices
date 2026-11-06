from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel


class SessionResponse(BaseModel):
    id: UUID
    booking_id: UUID
    user_id: UUID
    table_id: UUID
    start_at: AwareDatetime
    end_at: AwareDatetime
    status: Literal["expected", "active", "completed", "cancelled", "no_show"]
    started_at: AwareDatetime | None
    completed_at: AwareDatetime | None
