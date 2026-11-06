from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class PaymentResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["succeeded", "failed"]
    amount: int = Field(gt=0, strict=True, description="Сумма в копейках")
    currency: Literal["RUB"] = "RUB"


class RefundResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["succeeded", "failed"]


class PaymentResponse(BaseModel):
    id: UUID
    booking_id: UUID
    user_id: UUID
    amount: int
    currency: Literal["RUB"]
    status: Literal["pending", "failed", "succeeded"]
    refund_status: Literal["pending", "failed", "succeeded"] | None
    refunded_amount: int
    created_at: AwareDatetime
    payment_url: str
    provider: str
    refund_reason: str | None = None
