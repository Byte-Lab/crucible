# guest/protocol.py
from typing import Any
from pydantic import BaseModel


class GuestCommand(BaseModel):
    cmd: str
    groups: list[str] | None = None
    app_id: int | None = None
    args: list[str] | None = None
    config: dict[str, Any] | None = None
    events: list[dict[str, Any]] | None = None
    path: str | None = None


class GuestResponse(BaseModel):
    status: str
    data: dict[str, Any] | None = None
    message: str | None = None

    @classmethod
    def ok(cls, data: dict[str, Any] | None = None) -> "GuestResponse":
        return cls(status="ok", data=data or {})

    @classmethod
    def error(cls, message: str) -> "GuestResponse":
        return cls(status="error", message=message)
