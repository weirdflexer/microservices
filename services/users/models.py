"""Контракты профиля клиента."""
from pydantic import BaseModel, ConfigDict, Field


class CreateUser(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=100, description="Имя клиента")
    contact: str = Field(min_length=3, max_length=254, description="Email или телефон для связи")


class UpdateUser(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str | None = Field(default=None, min_length=1, max_length=100)
    contact: str | None = Field(default=None, min_length=3, max_length=254)


class UserResponse(BaseModel):
    id: str = Field(description="UUID клиента")
    name: str
    contact: str
    created_at: str = Field(description="Момент регистрации в UTC, ISO 8601")
