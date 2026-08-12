"""Core data models for the effect ledger."""

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


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


class EvidenceKind(Enum):
    """How an evidence claim is grounded.

    The kind describes the trust boundary; it does not by itself authenticate
    the source or prove that the referenced record is truthful.
    """

    ENFORCED = "enforced"
    ATTESTED = "attested"
    OBSERVED = "observed"


@dataclass(frozen=True)
class EvidenceReference:
    """Structured provenance for a reconciliation claim.

    ``subject_idempotency_key`` binds the evidence to the effect being
    reconciled. ``artifact_digest`` binds it to the referenced record's
    contents. Provider authentication remains the reconciler's trust boundary.
    """

    kind: EvidenceKind
    source: str
    record_id: str
    subject_idempotency_key: str
    artifact_digest: str
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        """Validate evidence provenance and binding fields."""
        if not isinstance(self.kind, EvidenceKind):
            raise ValueError("kind must be an EvidenceKind")
        for field_name in ("source", "record_id", "subject_idempotency_key"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-blank string")
        if not isinstance(self.artifact_digest, str) or not re.fullmatch(
            r"[0-9a-fA-F]{64}", self.artifact_digest
        ):
            raise ValueError("artifact_digest must be a 64-character SHA-256 hex digest")
        if not isinstance(self.observed_at, datetime) or self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be a timezone-aware datetime")

    def to_json(self) -> str:
        """Return a stable JSON representation for durable storage."""
        return json.dumps(
            {
                "artifact_digest": self.artifact_digest.lower(),
                "kind": self.kind.value,
                "observed_at": self.observed_at.isoformat(),
                "record_id": self.record_id,
                "source": self.source,
                "subject_idempotency_key": self.subject_idempotency_key,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, value: str) -> "EvidenceReference":
        """Rebuild a validated evidence reference from durable storage."""
        data = json.loads(value)
        if not isinstance(data, dict):
            raise ValueError("stored evidence reference must be a JSON object")
        return cls(
            kind=EvidenceKind(data["kind"]),
            source=data["source"],
            record_id=data["record_id"],
            subject_idempotency_key=data["subject_idempotency_key"],
            artifact_digest=data["artifact_digest"],
            observed_at=datetime.fromisoformat(data["observed_at"]),
        )

    def __str__(self) -> str:
        """Return a concise human-readable locator."""
        return f"{self.kind.value}:{self.source}:{self.record_id}"


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
        if not isinstance(self.payload_digest, str) or not re.fullmatch(
            r"[0-9a-fA-F]{64}", self.payload_digest
        ):
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
            if (
                not isinstance(self.disposition_changed_at, datetime)
                or self.disposition_changed_at.tzinfo is None
            ):
                raise ValueError("disposition_changed_at must be a timezone-aware datetime or None")


@dataclass(frozen=True)
class LedgerEvent:
    """Append-only event in the ledger timeline."""

    sequence: int
    effect_id: str
    event_type: str
    timestamp: datetime
    evidence_reference: EvidenceReference | None = None
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
        if self.evidence_reference is not None and not isinstance(
            self.evidence_reference, EvidenceReference
        ):
            raise ValueError("evidence_reference must be an EvidenceReference or None")


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
    reconciliation_evidence: EvidenceReference | None = None

    def __post_init__(self) -> None:
        """Validate the effect fields."""
        if not isinstance(self.intent, EffectIntent):
            raise ValueError("intent must be an EffectIntent")
        if not isinstance(self.status, EffectStatus):
            raise ValueError("status must be an EffectStatus")
        if not isinstance(self.mode, ExecutionMode):
            raise ValueError("mode must be an ExecutionMode")
