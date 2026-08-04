from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
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


@dataclass(frozen=True, slots=True)
class TokenReading:
    """A validated daily token-usage reading from the Codex account/usage/read surface.

    Represents one daily bucket (``dailyUsageBuckets[{startDate,tokens}]``).
    The single ``tokens`` field is the official daily total — no input/output/
    cache/reasoning breakdown is exposed. Summary fields (lifetime, peak, streak,
    longest-turn) are provider aggregates stored separately from daily buckets.

    Token availability is independent of quota percentage status.
    AVAILABLE_EXACT carries a non-negative integer tokens value.
    Other statuses carry no counts.
    """

    service: Service
    day: date
    retrieved_at: datetime
    source: str
    status: str  # HistoryStatus value
    tokens: int | None = None
    summary_lifetime: int | None = None
    summary_peak: int | None = None
    summary_streak: int | None = None
    summary_longest_turn: int | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        if self.retrieved_at.tzinfo is None:
            raise ValueError("retrieved_at must be timezone-aware")
        if not self.source.strip():
            raise ValueError("source must not be empty")
        if self.status == "available_exact":
            if self.tokens is None:
                raise ValueError("AVAILABLE_EXACT token readings require tokens")
        else:
            if any(
                v is not None
                for v in (
                    self.tokens,
                    self.summary_lifetime,
                    self.summary_peak,
                    self.summary_streak,
                    self.summary_longest_turn,
                )
            ):
                raise ValueError("non-available token readings must not carry counts")
        for name, value in (
            ("tokens", self.tokens),
            ("summary_lifetime", self.summary_lifetime),
            ("summary_peak", self.summary_peak),
            ("summary_streak", self.summary_streak),
            ("summary_longest_turn", self.summary_longest_turn),
        ):
            if value is not None:
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ValueError(f"{name} must be a non-negative integer")

    @property
    def available(self) -> bool:
        """Return True when exact tokens are available."""
        return self.status == "available_exact" and self.tokens is not None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "service": self.service.value,
            "day": self.day.isoformat(),
            "retrieved_at": self.retrieved_at.isoformat(),
            "source": self.source,
            "status": self.status,
            "detail": self.detail,
        }
        for field in (
            "tokens",
            "summary_lifetime",
            "summary_peak",
            "summary_streak",
            "summary_longest_turn",
        ):
            value = getattr(self, field)
            if value is not None:
                data[field] = value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TokenReading:
        return cls(
            service=Service(data["service"]),
            day=date.fromisoformat(data["day"]),
            retrieved_at=datetime.fromisoformat(data["retrieved_at"]),
            source=str(data["source"]),
            status=str(data["status"]),
            tokens=data.get("tokens"),
            summary_lifetime=data.get("summary_lifetime"),
            summary_peak=data.get("summary_peak"),
            summary_streak=data.get("summary_streak"),
            summary_longest_turn=data.get("summary_longest_turn"),
            detail=str(data.get("detail", "")),
        )


@dataclass(frozen=True, slots=True)
class CollectorResult:
    """Typed output from a single collector refresh.

    Carries both quota readings and token readings. Token readings are only
    present when the provider exposes a structured token surface.
    codex_summary carries the official aggregate summary (lifetime, peak,
    streak, longest-turn) when available from the usage surface.
    """

    quota_readings: tuple[QuotaReading, ...]
    token_readings: tuple[TokenReading, ...]
    codex_summary: dict[str, object] | None = None
