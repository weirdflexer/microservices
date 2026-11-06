"""Карточки бильярдных столов, без сведений о занятости."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

GameType = Literal["pool", "russian", "snooker"]


class CreateTable(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    number: int = Field(gt=0, description="Уникальный номер стола в клубе")
    game_type: GameType = Field(description="Вид игры")
    description: str = Field(default="", max_length=1000)
    published: bool = Field(default=False, description="Можно ли бронировать стол")


class UpdateTable(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    number: int | None = Field(default=None, gt=0)
    game_type: GameType | None = None
    description: str | None = Field(default=None, max_length=1000)
    published: bool | None = None


class TableResponse(BaseModel):
    id: str = Field(description="UUID стола")
    number: int
    game_type: GameType
    description: str
    published: bool
