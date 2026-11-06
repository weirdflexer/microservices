from uuid import UUID
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class ScheduleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    opens_at: AwareDatetime = Field(description="Начало рабочего окна стола, с часовым поясом")
    closes_at: AwareDatetime = Field(description="Конец рабочего окна стола, с часовым поясом")


class HoldRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    booking_id: UUID = Field(description="Бронь, которой принадлежит резерв")
    table_id: UUID
    start_at: AwareDatetime
    end_at: AwareDatetime


class ConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    booking_id: UUID


class ScheduleResponse(BaseModel):
    table_id: str
    opens_at: str
    closes_at: str


class HoldResponse(BaseModel):
    id: str = Field(description="UUID удержания")
    booking_id: str
    table_id: str
    start_at: str
    end_at: str
    expires_at: str = Field(description="Крайний срок подтверждения временного удержания")
    status: str = Field(description="held, confirmed, released или expired")


class AvailabilityResponse(BaseModel):
    table_id: str
    start_at: str
    end_at: str
    available: bool
    busy_intervals: list[HoldResponse]
