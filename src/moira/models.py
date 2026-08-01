from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class Service(StrEnum):
    CLAUDE = "claude"
    CODEX = "codex"


class QuotaStatus(StrEnum):
    AVAILABLE = "available"
    LOADING = "loading"
    UNAVAILABLE = "unavailable"
    STALE = "stale"
    PARSE_ERROR = "parse_error"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class QuotaReading:
    service: Service
    quota_label: str
    percentage: float | None
    reset_at: datetime | None
    retrieved_at: datetime
    source: str
    status: QuotaStatus
    detail: str = ""

    def __post_init__(self) -> None:
        if not self.quota_label.strip():
            raise ValueError("quota label must not be empty")
        if self.percentage is not None and not 0 <= self.percentage <= 100:
            raise ValueError("percentage must be between 0 and 100")
        for value, name in ((self.reset_at, "reset_at"), (self.retrieved_at, "retrieved_at")):
            if value is not None and value.tzinfo is None:
                raise ValueError(f"{name} must be timezone-aware")
        if self.status in {QuotaStatus.AVAILABLE, QuotaStatus.STALE}:
            if self.percentage is None or self.reset_at is None:
                raise ValueError("available and stale readings require values")

    def stale(self, detail: str) -> QuotaReading:
        if self.percentage is None or self.reset_at is None:
            raise ValueError("cannot make an empty reading stale")
        return QuotaReading(
            self.service,
            self.quota_label,
            self.percentage,
            self.reset_at,
            self.retrieved_at,
            self.source,
            QuotaStatus.STALE,
            detail,
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["service"] = self.service.value
        data["status"] = self.status.value
        data["reset_at"] = self.reset_at.isoformat() if self.reset_at else None
        data["retrieved_at"] = self.retrieved_at.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> QuotaReading:
        reset = data.get("reset_at")
        return cls(
            service=Service(data["service"]),
            quota_label=str(data["quota_label"]),
            percentage=float(data["percentage"]) if data.get("percentage") is not None else None,
            reset_at=datetime.fromisoformat(reset) if reset else None,
            retrieved_at=datetime.fromisoformat(data["retrieved_at"]),
            source=str(data["source"]),
            status=QuotaStatus(data["status"]),
            detail=str(data.get("detail", "")),
        )


def utc_now() -> datetime:
    return datetime.now(UTC)
