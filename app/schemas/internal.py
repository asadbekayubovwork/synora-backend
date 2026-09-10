"""Shapes for the microservice-facing API.

Every response carries `server_time`. It is the clock-skew canary: signed
requests are rejected outside a fixed window, and a service whose clock has
drifted gets a 401 that looks like a credential problem unless it can see ours.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator

from app.schemas.common import _Schema, ensure_utc


class _InternalResponse(_Schema):
    ok: bool = True
    server_time: datetime = Field(
        description="Our clock, RFC 3339 UTC. Alarm if it differs from yours by >2s.",
        examples=["2026-09-07T09:14:03.221Z"],
    )

    @field_validator("server_time")
    @classmethod
    def _ensure_utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class InternalHealthResponse(_InternalResponse):
    state: Literal["ready", "degraded"] = Field(
        description="`degraded` means gating counters are coming from Postgres, not Redis.",
        examples=["ready"],
    )
    price_book_version: int | None = Field(
        default=None,
        description="Null means nothing is published and nothing can be billed.",
        examples=[1],
    )
    redis: Literal["up", "down"] = Field(examples=["down"])


class EchoSignatureResponse(_InternalResponse):
    canonical: str = Field(
        description="The exact newline-joined string we signed. Diff yours against it.",
        examples=["SYNORA-HMAC-V1\nPOST\n/internal/v1/usage/events\n\n1757250000\n..."],
    )
    body_sha256: str = Field(
        description="sha256 of the raw request bytes, lowercase hex.",
        examples=["a3f1c9de7b2..."],
    )
    key_id: str = Field(examples=["svc_voice_agent_7f3a1c9e"])
    scopes: list[str] = Field(examples=[["usage:write", "sessions:report"]])
    signature_matched: bool = Field(examples=[True])
