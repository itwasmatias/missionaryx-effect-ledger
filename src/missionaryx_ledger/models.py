"""Core data models for the effect ledger."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import UUID, uuid4


class EffectStatus(Enum):
    """Status of an effect in the external world."""

    COMMITTED_NOT_DISPATCHED = "committed_not_dispatched"
    DISPATCHED_UNCONFIRMED = "dispatched_unconfirmed"
    NOTHING_LANDED = "nothing_landed"
    SOMETHING_LANDED = "something_landed"
    INDETERMINATE = "indeterminate"


class ExecutionMode(Enum):
    """Execution mode governing what the system may do next."""

    ACTIVE = "active"
    FAIL_SAFE_PLAN_MODE = "fail_safe_plan_mode"


class AuthorityDisposition(Enum):
    """Disposition of authority for an effect."""

    RESERVED = "reserved"
    CONSUMED = "consumed"
    RELEASED = "released"
    HELD_UNRECONCILED = "held_unreconciled"


@dataclass(frozen=True)
class EffectIntent:
    """Immutable intent to perform a real-world effect.

    This must be committed before dispatch. It represents the system's
    commitment to attempt an external action with specific authority.
    """

    effect_id: str
    mission_id: str
    authority_id: str
    operation: str
    target: str
    payload_digest: str  # SHA-256, never raw payload
    idempotency_key: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        """Validate the intent fields."""
        if not self.effect_id or not isinstance(self.effect_id, str):
            raise ValueError("effect_id must be a non-empty string")
        if not self.mission_id:
            raise ValueError("mission_id must be non-empty")
        if not self.authority_id:
            raise ValueError("authority_id must be non-empty")
        if not self.operation:
            raise ValueError("operation must be non-empty")
        if not self.target:
            raise ValueError("target must be non-empty")
        if not self.payload_digest or len(self.payload_digest) != 64:
            raise ValueError("payload_digest must be a 64-character SHA-256 hex digest")
        if not self.idempotency_key:
            raise ValueError("idempotency_key must be non-empty")
        if not isinstance(self.created_at, datetime) or self.created_at.tzinfo is None:
            raise ValueError("created_at must be a timezone-aware datetime")


@dataclass(frozen=True)
class AuthorityReservation:
    """Record of authority reserved for an effect."""

    authority_id: str
    effect_id: str
    disposition: AuthorityDisposition
    reserved_at: datetime
    disposition_changed_at: datetime | None = None

    def __post_init__(self) -> None:
        """Validate the reservation fields."""
        if not self.authority_id:
            raise ValueError("authority_id must be non-empty")
        if not self.effect_id:
            raise ValueError("effect_id must be non-empty")
        if not isinstance(self.disposition, AuthorityDisposition):
            raise ValueError("disposition must be an AuthorityDisposition")
        if not isinstance(self.reserved_at, datetime) or self.reserved_at.tzinfo is None:
            raise ValueError("reserved_at must be a timezone-aware datetime")
        if self.disposition_changed_at is not None:
            if not isinstance(self.disposition_changed_at, datetime) or self.disposition_changed_at.tzinfo is None:
                raise ValueError("disposition_changed_at must be a timezone-aware datetime or None")


@dataclass(frozen=True)
class LedgerEvent:
    """Append-only event in the ledger timeline."""

    sequence: int
    effect_id: str
    event_type: str
    timestamp: datetime
    evidence_reference: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate the event fields."""
        if self.sequence < 1:
            raise ValueError("sequence must be positive")
        if not self.effect_id:
            raise ValueError("effect_id must be non-empty")
        if not self.event_type:
            raise ValueError("event_type must be non-empty")
        if not isinstance(self.timestamp, datetime) or self.timestamp.tzinfo is None:
            raise ValueError("timestamp must be a timezone-aware datetime")
        # evidence_reference must be non-empty if provided
        if self.evidence_reference is not None and not self.evidence_reference:
            raise ValueError("evidence_reference must be non-empty string or None")


@dataclass
class Effect:
    """Mutable effect state tracked by the ledger.

    This is the internal representation. Most external interactions
    use EffectIntent for the immutable committed portion.
    """

    intent: EffectIntent
    status: EffectStatus
    mode: ExecutionMode
    dispatched_at: datetime | None = None
    resolved_at: datetime | None = None
    reconciliation_evidence: str | None = None

    def __post_init__(self) -> None:
        """Validate the effect fields."""
        if not isinstance(self.intent, EffectIntent):
            raise ValueError("intent must be an EffectIntent")
        if not isinstance(self.status, EffectStatus):
            raise ValueError("status must be an EffectStatus")
        if not isinstance(self.mode, ExecutionMode):
            raise ValueError("mode must be an ExecutionMode")
