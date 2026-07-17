# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 David Vernet

# guest/protocol.py
#
# Stdlib-only on purpose. The Debian bookworm rootfs ships pydantic v1,
# which doesn't have model_validate / model_dump; sticking to dataclasses
# keeps the guest agent buildable on the same rootfs the orchestrator
# provisions without pulling pydantic v2 via pip.
import json
from dataclasses import dataclass, field, fields
from typing import Any


@dataclass
class GuestCommand:
    cmd: str
    groups: list[str] | None = None
    app_id: int | None = None
    args: list[str] | None = None
    config: dict[str, Any] | None = None
    events: list[dict[str, Any]] | None = None
    path: str | None = None
    name: str | None = None
    duration_secs: int | None = None
    mangohud_output: str | None = None
    coload_cpu: int | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "GuestCommand":
        known = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in raw.items() if k in known}
        return cls(**kwargs)

    @classmethod
    def from_json(cls, raw: str | bytes) -> "GuestCommand":
        return cls.from_dict(json.loads(raw))

    def to_dict(self, exclude_none: bool = False) -> dict[str, Any]:
        d = {f.name: getattr(self, f.name) for f in fields(self)}
        if exclude_none:
            d = {k: v for k, v in d.items() if v is not None}
        return d

    def to_json(self, exclude_none: bool = False) -> str:
        return json.dumps(self.to_dict(exclude_none=exclude_none))


@dataclass
class GuestResponse:
    status: str
    data: dict[str, Any] | None = None
    message: str | None = None

    @classmethod
    def ok(cls, data: dict[str, Any] | None = None) -> "GuestResponse":
        return cls(status="ok", data=data or {})

    @classmethod
    def error(cls, message: str) -> "GuestResponse":
        return cls(status="error", message=message)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "GuestResponse":
        known = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in raw.items() if k in known}
        return cls(**kwargs)

    def to_dict(self, exclude_none: bool = False) -> dict[str, Any]:
        d = {f.name: getattr(self, f.name) for f in fields(self)}
        if exclude_none:
            d = {k: v for k, v in d.items() if v is not None}
        return d

    def to_json(self, exclude_none: bool = False) -> str:
        return json.dumps(self.to_dict(exclude_none=exclude_none))
