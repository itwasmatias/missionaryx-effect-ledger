"""Simulated email provider with deterministic scenarios.

This provider never makes network calls. It simulates different
scenarios for testing and demonstration.
"""

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from missionaryx_ledger.models import EvidenceKind, EvidenceReference, EffectIntent, EffectStatus
from missionaryx_ledger.reconciliation import ReconciliationResult, Reconciler


@dataclass
class SimulatedDelivery:
    """Record of a simulated email delivery."""

    idempotency_key: str
    target: str
    delivered_at: datetime
    delivery_id: str


ScenarioType = Literal["invalid_address", "accepted", "connection_loss_after_possible_acceptance"]


class SimulatedEmailProvider(Reconciler):
    """Simulated email provider for testing and demonstration.

    Supports three scenarios:
    - invalid_address: Immediate rejection
    - accepted: Successful delivery
    - connection_loss_after_possible_acceptance: Indeterminate then eventual resolution
    """

    def __init__(self, scenario: ScenarioType):
        """Initialize with a specific scenario.

        Args:
            scenario: Which scenario to simulate
        """
        self.scenario = scenario
        self._deliveries: dict[str, SimulatedDelivery] = {}
        self._dispatch_attempts: dict[str, int] = {}

    @staticmethod
    def _evidence(
        intent: EffectIntent,
        record_id: str,
        artifact: str,
        *,
        kind: EvidenceKind = EvidenceKind.ATTESTED,
        observed_at: datetime | None = None,
    ) -> EvidenceReference:
        """Build structured, effect-bound evidence for a simulated record."""
        return EvidenceReference(
            kind=kind,
            source="simulated-email-provider",
            record_id=record_id,
            subject_idempotency_key=intent.idempotency_key,
            artifact_digest=hashlib.sha256(artifact.encode("utf-8")).hexdigest(),
            observed_at=observed_at or datetime.now(timezone.utc),
        )

    def dispatch(self, intent: EffectIntent) -> tuple[bool, str | None]:
        """Simulate dispatching an email.

        Args:
            intent: The effect intent

        Returns:
            Tuple of (success, error_message)
        """
        # Track attempt
        self._dispatch_attempts[intent.idempotency_key] = (
            self._dispatch_attempts.get(intent.idempotency_key, 0) + 1
        )

        if self.scenario == "invalid_address":
            return False, "Provider rejected the supplied address as invalid"

        elif self.scenario == "accepted":
            # Record delivery
            delivery_id = f"delivery-{intent.idempotency_key}"
            self._deliveries[intent.idempotency_key] = SimulatedDelivery(
                idempotency_key=intent.idempotency_key,
                target=intent.target,
                delivered_at=datetime.now(timezone.utc),
                delivery_id=delivery_id,
            )
            return True, None

        elif self.scenario == "connection_loss_after_possible_acceptance":
            # Simulate possible delivery but connection lost before confirmation
            # 50% chance it was actually delivered (based on attempt count for determinism)
            attempt = self._dispatch_attempts[intent.idempotency_key]
            if attempt % 2 == 1:
                # Odd attempts: actually deliver but don't confirm
                delivery_id = f"delivery-{intent.idempotency_key}"
                self._deliveries[intent.idempotency_key] = SimulatedDelivery(
                    idempotency_key=intent.idempotency_key,
                    target=intent.target,
                    delivered_at=datetime.now(timezone.utc),
                    delivery_id=delivery_id,
                )
            # Connection lost - no confirmation
            raise ConnectionError("Connection lost after possible acceptance")

        else:
            raise ValueError(f"Unknown scenario: {self.scenario}")

    def reconcile(self, intent: EffectIntent) -> ReconciliationResult:
        """Reconcile the true state of an email delivery.

        Args:
            intent: The effect intent to reconcile

        Returns:
            ReconciliationResult with finding and evidence
        """
        if self.scenario == "invalid_address":
            # Provider rejected immediately - provably not delivered
            return ReconciliationResult(
                finding=EffectStatus.NOTHING_LANDED,
                evidence_reference=self._evidence(
                    intent,
                    record_id=f"provider-rejection-log:{intent.idempotency_key}",
                    artifact=f"rejected|invalid-address|{intent.idempotency_key}",
                ),
                explanation="Provider attested that the supplied address was rejected as invalid.",
            )

        elif self.scenario == "accepted":
            # Check delivery records
            delivery = self._deliveries.get(intent.idempotency_key)
            if delivery:
                return ReconciliationResult(
                    finding=EffectStatus.SOMETHING_LANDED,
                    evidence_reference=self._evidence(
                        intent,
                        record_id=f"delivery-receipt:{delivery.delivery_id}",
                        artifact=(
                            f"delivered|{delivery.delivery_id}|"
                            f"{delivery.delivered_at.isoformat()}"
                        ),
                        observed_at=delivery.delivered_at,
                    ),
                    explanation=(
                        f"Provider attested delivery at "
                        f"{delivery.delivered_at.isoformat()}."
                    ),
                    metadata={"delivery_id": delivery.delivery_id},
                )
            else:
                # Not found in delivery records - nothing landed
                return ReconciliationResult(
                    finding=EffectStatus.NOTHING_LANDED,
                    evidence_reference=self._evidence(
                        intent,
                        record_id=f"provider-query-log:not-found:{intent.idempotency_key}",
                        artifact=f"not-found|{intent.idempotency_key}",
                    ),
                    explanation="Provider attested that no matching delivery record exists.",
                )

        elif self.scenario == "connection_loss_after_possible_acceptance":
            # Check if it was actually delivered
            delivery = self._deliveries.get(intent.idempotency_key)
            if delivery:
                return ReconciliationResult(
                    finding=EffectStatus.SOMETHING_LANDED,
                    evidence_reference=self._evidence(
                        intent,
                        record_id=f"delayed-delivery-confirmation:{delivery.delivery_id}",
                        artifact=(
                            f"delayed-confirmation|{delivery.delivery_id}|"
                            f"{delivery.delivered_at.isoformat()}"
                        ),
                        observed_at=delivery.delivered_at,
                    ),
                    explanation="Provider supplied delayed confirmation that delivery occurred.",
                    metadata={"delivery_id": delivery.delivery_id},
                )
            else:
                # Still cannot prove either way - remains indeterminate
                # In a real system, this might happen for a while before eventual resolution
                return ReconciliationResult(
                    finding=EffectStatus.INDETERMINATE,
                    evidence_reference=self._evidence(
                        intent,
                        record_id="provider-availability-check:unavailable",
                        artifact=f"provider-unavailable|{intent.idempotency_key}",
                        kind=EvidenceKind.OBSERVED,
                    ),
                    explanation="Provider still unavailable - cannot confirm delivery status",
                )

        else:
            raise ValueError(f"Unknown scenario: {self.scenario}")
