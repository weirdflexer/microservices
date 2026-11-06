from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict


class BookingCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: UUID
    table_id: UUID
    quote_id: UUID
    start_at: AwareDatetime
    end_at: AwareDatetime


class BookingResponse(BaseModel):
    id: UUID
    user_id: UUID
    table_id: UUID
    quote_id: UUID
    start_at: AwareDatetime
    end_at: AwareDatetime
    amount: int
    currency: Literal["RUB"]
    hold_id: UUID
    expires_at: AwareDatetime
    status: Literal["waiting_payment", "confirmed", "cancelled", "expired", "rejected", "completed", "no_show"]
    payment_id: UUID | None
    payment_url: str | None
    paid: bool
    refund_status: Literal["pending", "failed", "succeeded"] | None
    payment_attempts: list[str]
    created_at: AwareDatetime
    cancelled_at: AwareDatetime | None = None
