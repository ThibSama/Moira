from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any

# Signed 64-bit integer upper bound (schema int64).
INT64_MAX = 2**63 - 1


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


class HistoryStatus(StrEnum):
    """Typed availability state for a telemetry observation.

    Provider-neutral closed set. Sanitized by construction — the enum
    values are the only strings that may flow through the system, so
    arbitrary free-form status strings are impossible.
    """

    AVAILABLE_EXACT = "available_exact"
    UNSUPPORTED = "unsupported"
    TEMPORARILY_UNAVAILABLE = "temporarily_unavailable"
    INVALID = "invalid"


def _validate_int64(value: int | None, field_name: str) -> int | None:
    """Validate a nullable signed-int64 field. Booleans, floats, negatives
    and values above INT64_MAX fail closed."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer")
    if value < 0:
        raise ValueError(f"{field_name} must be non-negative")
    if value > INT64_MAX:
        raise ValueError(f"{field_name} exceeds signed int64")
    return value


@dataclass(frozen=True, slots=True)
class CodexSummary:
    """Official Codex account/usage/read summary block (typed, immutable).

    Mirrors the generated protocol schema: ``summary`` is a required object
    whose five fields are nullable int64 values:

    - ``lifetimeTokens``          → lifetime_tokens
    - ``peakDailyTokens``         → peak_daily_tokens
    - ``currentStreakDays``       → current_streak_days
    - ``longestStreakDays``       → longest_streak_days
    - ``longestRunningTurnSec``   → longest_running_turn_sec

    Provider-neutral: the model carries only the service, the source surface,
    the retrieval instant, and the five aggregate fields. It is persisted as
    one typed record per refresh — never duplicated onto daily buckets.
    """

    service: Service
    source: str
    observed_at: datetime
    lifetime_tokens: int | None = None
    peak_daily_tokens: int | None = None
    current_streak_days: int | None = None
    longest_streak_days: int | None = None
    longest_running_turn_sec: int | None = None

    def __post_init__(self) -> None:
        if not self.source.strip():
            raise ValueError("source must not be empty")
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        for name in (
            "lifetime_tokens",
            "peak_daily_tokens",
            "current_streak_days",
            "longest_streak_days",
            "longest_running_turn_sec",
        ):
            object.__setattr__(self, name, _validate_int64(getattr(self, name), name))

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "service": self.service.value,
            "source": self.source,
            "observed_at": self.observed_at.isoformat(),
        }
        for name in (
            "lifetime_tokens",
            "peak_daily_tokens",
            "current_streak_days",
            "longest_streak_days",
            "longest_running_turn_sec",
        ):
            value = getattr(self, name)
            if value is not None:
                data[name] = value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CodexSummary:
        return cls(
            service=Service(data["service"]),
            source=str(data["source"]),
            observed_at=datetime.fromisoformat(data["observed_at"]),
            lifetime_tokens=data.get("lifetime_tokens"),
            peak_daily_tokens=data.get("peak_daily_tokens"),
            current_streak_days=data.get("current_streak_days"),
            longest_streak_days=data.get("longest_streak_days"),
            longest_running_turn_sec=data.get("longest_running_turn_sec"),
        )


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
    cache/reasoning breakdown is exposed. The official aggregate summary
    (lifetime, peak, streaks, longest turn) travels as a separate typed
    ``CodexSummary`` record, never duplicated onto daily buckets.

    Token availability is a typed ``HistoryStatus`` — no arbitrary status
    string. AVAILABLE_EXACT carries a non-negative int64 tokens value.
    Other statuses carry no counts.
    """

    service: Service
    day: date
    retrieved_at: datetime
    source: str
    status: HistoryStatus
    tokens: int | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        if self.retrieved_at.tzinfo is None:
            raise ValueError("retrieved_at must be timezone-aware")
        if not self.source.strip():
            raise ValueError("source must not be empty")
        if not isinstance(self.status, HistoryStatus):
            raise ValueError("status must be a HistoryStatus value")
        if self.status is HistoryStatus.AVAILABLE_EXACT:
            if self.tokens is None:
                raise ValueError("AVAILABLE_EXACT token readings require tokens")
        else:
            if self.tokens is not None:
                raise ValueError("non-available token readings must not carry counts")
        object.__setattr__(self, "tokens", _validate_int64(self.tokens, "tokens"))

    @property
    def available(self) -> bool:
        """Return True when exact tokens are available."""
        return self.status is HistoryStatus.AVAILABLE_EXACT and self.tokens is not None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "service": self.service.value,
            "day": self.day.isoformat(),
            "retrieved_at": self.retrieved_at.isoformat(),
            "source": self.source,
            "status": self.status.value,
            "detail": self.detail,
        }
        if self.tokens is not None:
            data["tokens"] = self.tokens
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TokenReading:
        return cls(
            service=Service(data["service"]),
            day=date.fromisoformat(data["day"]),
            retrieved_at=datetime.fromisoformat(data["retrieved_at"]),
            source=str(data["source"]),
            status=HistoryStatus(data["status"]),
            tokens=data.get("tokens"),
            detail=str(data.get("detail", "")),
        )


@dataclass(frozen=True, slots=True)
class CollectorResult:
    """Typed output from a single collector refresh.

    Carries both quota readings and token readings. Token readings are only
    present when the provider exposes a structured token surface.
    codex_summary carries the official aggregate summary (lifetime, peak,
    streaks, longest turn) as one typed immutable record when available
    from the usage surface.
    """

    quota_readings: tuple[QuotaReading, ...]
    token_readings: tuple[TokenReading, ...]
    codex_summary: CodexSummary | None = None
