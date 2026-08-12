"""Reconciliation protocol and results for effect resolution."""

from dataclasses import dataclass
from typing import Protocol

from missionaryx_ledger.models import EffectIntent, EffectStatus


@dataclass(frozen=True)
class ReconciliationResult:
    """Result of attempting to reconcile an effect's true state.

    The evidence_reference must be non-empty for terminal resolutions
    (nothing_landed or something_landed). It should point to provider
    records, logs, or other authoritative sources.
    """

    finding: EffectStatus  # Must be nothing_landed, something_landed, or indeterminate
    evidence_reference: str  # Required for terminal findings, describes the proof
    explanation: str  # Human-readable description
    metadata: dict[str, str] | None = None

    def __post_init__(self) -> None:
        """Validate the reconciliation result."""
        # Finding must be a terminal status or still indeterminate
        valid_findings = {
            EffectStatus.NOTHING_LANDED,
            EffectStatus.SOMETHING_LANDED,
            EffectStatus.INDETERMINATE,
        }
        if self.finding not in valid_findings:
            raise ValueError(
                f"Reconciliation finding must be nothing_landed, something_landed, "
                f"or indeterminate, got {self.finding}"
            )

        # Terminal findings require non-empty evidence
        if self.finding in {EffectStatus.NOTHING_LANDED, EffectStatus.SOMETHING_LANDED}:
            if not self.evidence_reference:
                raise ValueError(
                    f"Terminal finding {self.finding.value} requires non-empty evidence_reference"
                )

        if not self.explanation:
            raise ValueError("explanation must be non-empty")


class Reconciler(Protocol):
    """Protocol for effect reconciliation.

    Reconcilers examine provider state, logs, or other evidence to
    determine whether an effect actually occurred in the external world.
    """

    def reconcile(self, intent: EffectIntent) -> ReconciliationResult:
        """Reconcile the true state of an effect.

        Args:
            intent: The original effect intent to reconcile

        Returns:
            ReconciliationResult with finding and evidence

        The implementation should:
        - Check provider state using the idempotency key
        - Look for delivery receipts, operation logs, or confirmations
        - Return nothing_landed with evidence if provably not executed
        - Return something_landed with evidence if provably executed
        - Return indeterminate if proof remains unavailable
        """
        ...
