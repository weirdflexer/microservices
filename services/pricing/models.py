from typing import Literal
from uuid import UUID
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class PublishTariff(BaseModel):
    model_config = ConfigDict(extra="forbid")
    game_type: Literal["pool", "russian", "snooker"]
    hourly_rate: int = Field(gt=0, strict=True, description="Цена часа в копейках; только RUB")


class QuoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    table_id: UUID
    start_at: AwareDatetime = Field(description="Начало игры с часовым поясом; шаг 30 минут")
    end_at: AwareDatetime = Field(description="Конец игры; длительность от 1 до 4 часов")


class TariffResponse(BaseModel):
    id: str = Field(description="UUID версии тарифа")
    game_type: Literal["pool", "russian", "snooker"]
    hourly_rate: int = Field(description="Цена часа в копейках")
    version: int
    currency: Literal["RUB"]
    published_at: str = Field(description="Момент публикации в UTC")


class QuoteResponse(BaseModel):
    id: str = Field(description="UUID ценового предложения")
    table_id: str
    start_at: str
    end_at: str
    amount: int = Field(description="Зафиксированная цена всей игры в копейках")
    currency: Literal["RUB"]
    expires_at: str = Field(description="Предложение действительно строго до этого момента")
    tariff_id: str
    tariff_version: int
    hourly_rate: int = Field(description="Ставка исходной версии тарифа, в копейках")
