"""Reconciliation protocol and results for effect resolution."""

from dataclasses import dataclass
from typing import Protocol

from missionaryx_ledger.models import EvidenceReference, EffectIntent, EffectStatus


@dataclass(frozen=True)
class ReconciliationResult:
    """Result of attempting to reconcile an effect's true state.

    Terminal resolutions require a structured evidence reference. The ledger
    validates its shape and effect binding; the reconciler remains responsible
    for authenticating the provider and truthfully constructing the reference.
    """

    finding: EffectStatus  # Must be nothing_landed, something_landed, or indeterminate
    evidence_reference: EvidenceReference | None
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

        # Terminal findings require structured evidence provenance.
        if self.finding in {EffectStatus.NOTHING_LANDED, EffectStatus.SOMETHING_LANDED}:
            if not isinstance(self.evidence_reference, EvidenceReference):
                raise ValueError(
                    f"Terminal finding {self.finding.value} requires an EvidenceReference"
                )

        if self.evidence_reference is not None and not isinstance(
            self.evidence_reference, EvidenceReference
        ):
            raise ValueError("evidence_reference must be an EvidenceReference or None")

        if not isinstance(self.explanation, str) or not self.explanation.strip():
            raise ValueError("explanation must be a non-blank string")


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
