from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class NotificationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: UUID
    booking_id: UUID | None = None
    kind: Literal["custom", "confirmation", "reminder", "refund"] = "custom"
    message: str = Field(min_length=1, max_length=2000)
    scheduled_at: AwareDatetime | None = None


class NotificationResponse(BaseModel):
    id: UUID
    user_id: UUID
    booking_id: UUID | None
    kind: Literal["custom", "confirmation", "reminder", "refund"]
    message: str
    status: Literal["prepared", "sent", "failed", "cancelled"]
    attempts: int
    scheduled_at: AwareDatetime
    sent_at: AwareDatetime | None
    last_error: str | None
    provider: str
