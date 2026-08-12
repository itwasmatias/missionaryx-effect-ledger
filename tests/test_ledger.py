"""Comprehensive tests for the effect ledger.

These tests prove the core reliability invariants:
- Intent is durable before dispatch
- No duplicate authority or idempotency keys
- Proper state transitions
- Indeterminate effects cannot be retried
- Authority held correctly
- Events are append-only and chronological
"""

import hashlib
import sqlite3
import tempfile
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from pathlib import Path
from uuid import uuid4

import pytest

from missionaryx_ledger import (
    EffectIntent,
    EffectLedger,
    EffectStatus,
    ExecutionMode,
    AuthorityDisposition,
    AuthorityAlreadyUsedError,
    IdempotencyKeyConflictError,
    InvalidStateTransitionError,
    DispatchNotAllowedError,
    EvidenceKind,
    EvidenceReference,
    EffectNotFoundError,
    ReconciliationError,
)
from missionaryx_ledger.reconciliation import ReconciliationResult
from missionaryx_ledger.simulated_provider import SimulatedEmailProvider


@pytest.fixture
def ledger():
    """Create a temporary ledger for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    ledger = EffectLedger(db_path)
    yield ledger
    Path(db_path).unlink(missing_ok=True)


@pytest.fixture
def sample_intent():
    """Create a sample effect intent."""
    return EffectIntent(
        effect_id=f"test-{uuid4().hex[:8]}",
        mission_id="test-mission",
        authority_id=f"auth-{uuid4().hex[:8]}",
        operation="email.send",
        target="test@example.com",
        payload_digest=hashlib.sha256(b"test payload").hexdigest(),
        idempotency_key=f"idem-{uuid4().hex[:8]}",
    )


def evidence_for(
    intent: EffectIntent, record_id: str, *, subject_idempotency_key: str | None = None
) -> EvidenceReference:
    """Create valid structured evidence bound to an intent."""
    return EvidenceReference(
        kind=EvidenceKind.ATTESTED,
        source="test-provider",
        record_id=record_id,
        subject_idempotency_key=subject_idempotency_key or intent.idempotency_key,
        artifact_digest=hashlib.sha256(record_id.encode("utf-8")).hexdigest(),
    )


class StaticReconciler:
    """Return one prebuilt reconciliation result and count invocations."""

    def __init__(self, result: ReconciliationResult):
        self.result = result
        self.calls = 0

    def reconcile(self, intent: EffectIntent) -> ReconciliationResult:
        self.calls += 1
        return self.result


class BarrierReconciler(StaticReconciler):
    """Hold two reconciliations until both have completed preflight."""

    def __init__(self, result: ReconciliationResult, barrier: Barrier):
        super().__init__(result)
        self.barrier = barrier

    def reconcile(self, intent: EffectIntent) -> ReconciliationResult:
        self.calls += 1
        self.barrier.wait(timeout=5)
        return self.result


class TestIntentCommitment:
    """Test that intent is durable before dispatch is allowed."""

    def test_intent_committed_before_dispatch(self, ledger, sample_intent):
        """Intent must be committed before dispatch can be recorded."""
        # Commit intent
        ledger.commit_intent(sample_intent)

        # Verify effect exists and is in correct state
        effect = ledger.get_effect(sample_intent.effect_id)
        assert effect.status == EffectStatus.COMMITTED_NOT_DISPATCHED
        assert effect.mode == ExecutionMode.ACTIVE

        # Verify authority is reserved
        auth = ledger.get_authority(sample_intent.effect_id)
        assert auth.disposition == AuthorityDisposition.RESERVED

    def test_cannot_dispatch_nonexistent_effect(self, ledger):
        """Cannot dispatch an effect that hasn't been committed."""
        with pytest.raises(EffectNotFoundError):
            ledger.record_dispatch("nonexistent-effect")

    def test_payload_digest_must_be_hex(self):
        """A 64-character non-hex string is not a SHA-256 digest."""
        with pytest.raises(ValueError, match="SHA-256 hex digest"):
            EffectIntent(
                effect_id="effect-bad-digest",
                mission_id="test-mission",
                authority_id="auth-bad-digest",
                operation="email.send",
                target="test@example.com",
                payload_digest="z" * 64,
                idempotency_key="idem-bad-digest",
            )


class TestStateTransitions:
    """Test legal and illegal state transitions."""

    def test_dispatch_only_from_committed(self, ledger, sample_intent):
        """Dispatch can only be recorded from committed_not_dispatched."""
        ledger.commit_intent(sample_intent)
        ledger.record_dispatch(sample_intent.effect_id)

        effect = ledger.get_effect(sample_intent.effect_id)
        assert effect.status == EffectStatus.DISPATCHED_UNCONFIRMED

    def test_cannot_dispatch_twice(self, ledger, sample_intent):
        """Cannot record dispatch twice."""
        ledger.commit_intent(sample_intent)
        ledger.record_dispatch(sample_intent.effect_id)

        with pytest.raises(InvalidStateTransitionError):
            ledger.record_dispatch(sample_intent.effect_id)

    def test_indeterminate_only_after_dispatch(self, ledger, sample_intent):
        """Can only mark indeterminate after dispatch."""
        ledger.commit_intent(sample_intent)

        with pytest.raises(InvalidStateTransitionError):
            ledger.mark_indeterminate(sample_intent.effect_id, "test reason")

    def test_indeterminate_reason_must_not_be_blank(self, ledger, sample_intent):
        ledger.commit_intent(sample_intent)
        ledger.record_dispatch(sample_intent.effect_id)

        with pytest.raises(ValueError, match="non-blank"):
            ledger.mark_indeterminate(sample_intent.effect_id, "   ")

        assert (
            ledger.get_effect(sample_intent.effect_id).status
            == EffectStatus.DISPATCHED_UNCONFIRMED
        )


class TestAuthorityReservations:
    """Test authority double-spend prevention."""

    def test_same_authority_cannot_create_second_effect(self, ledger):
        """Authority ID can only be used once."""
        authority_id = f"auth-{uuid4().hex[:8]}"

        intent1 = EffectIntent(
            effect_id="effect-1",
            mission_id="test-mission",
            authority_id=authority_id,
            operation="email.send",
            target="test1@example.com",
            payload_digest=hashlib.sha256(b"payload1").hexdigest(),
            idempotency_key="idem-1",
        )

        intent2 = EffectIntent(
            effect_id="effect-2",
            mission_id="test-mission",
            authority_id=authority_id,  # Same authority!
            operation="email.send",
            target="test2@example.com",
            payload_digest=hashlib.sha256(b"payload2").hexdigest(),
            idempotency_key="idem-2",
        )

        ledger.commit_intent(intent1)

        with pytest.raises(AuthorityAlreadyUsedError) as exc_info:
            ledger.commit_intent(intent2)

        assert exc_info.value.authority_id == authority_id
        assert exc_info.value.existing_effect_id == "effect-1"

    def test_authority_consumed_on_something_landed(self, ledger, sample_intent):
        """Authority is consumed when effect lands."""
        ledger.commit_intent(sample_intent)
        ledger.record_dispatch(sample_intent.effect_id)

        provider = SimulatedEmailProvider("accepted")
        provider.dispatch(sample_intent)  # Simulate dispatch
        ledger.reconcile(sample_intent.effect_id, provider)

        auth = ledger.get_authority(sample_intent.effect_id)
        assert auth.disposition == AuthorityDisposition.CONSUMED

    def test_authority_released_on_nothing_landed(self, ledger, sample_intent):
        """Authority is released when nothing lands."""
        intent = EffectIntent(
            effect_id=f"test-{uuid4().hex[:8]}",
            mission_id="test-mission",
            authority_id=f"auth-{uuid4().hex[:8]}",
            operation="email.send",
            target="invalid@@bad",
            payload_digest=hashlib.sha256(b"test").hexdigest(),
            idempotency_key=f"idem-{uuid4().hex[:8]}",
        )

        ledger.commit_intent(intent)
        ledger.record_dispatch(intent.effect_id)

        provider = SimulatedEmailProvider("invalid_address")
        ledger.reconcile(intent.effect_id, provider)

        auth = ledger.get_authority(intent.effect_id)
        assert auth.disposition == AuthorityDisposition.RELEASED

    def test_authority_held_on_indeterminate(self, ledger, sample_intent):
        """Authority is held when effect becomes indeterminate."""
        ledger.commit_intent(sample_intent)
        ledger.record_dispatch(sample_intent.effect_id)
        ledger.mark_indeterminate(sample_intent.effect_id, "connection lost")

        auth = ledger.get_authority(sample_intent.effect_id)
        assert auth.disposition == AuthorityDisposition.HELD_UNRECONCILED


class TestIdempotency:
    """Test idempotency key enforcement."""

    def test_same_idempotency_key_rejected(self, ledger):
        """Idempotency key can only be used once."""
        idem_key = f"idem-{uuid4().hex[:8]}"

        intent1 = EffectIntent(
            effect_id="effect-1",
            mission_id="test-mission",
            authority_id="auth-1",
            operation="email.send",
            target="test1@example.com",
            payload_digest=hashlib.sha256(b"payload1").hexdigest(),
            idempotency_key=idem_key,
        )

        intent2 = EffectIntent(
            effect_id="effect-2",
            mission_id="test-mission",
            authority_id="auth-2",
            operation="email.send",
            target="test2@example.com",
            payload_digest=hashlib.sha256(b"payload2").hexdigest(),
            idempotency_key=idem_key,  # Same key!
        )

        ledger.commit_intent(intent1)

        with pytest.raises(IdempotencyKeyConflictError) as exc_info:
            ledger.commit_intent(intent2)

        assert exc_info.value.idempotency_key == idem_key
        assert exc_info.value.existing_effect_id == "effect-1"


class TestReconciliation:
    """Test reconciliation scenarios."""

    def test_invalid_address_yields_nothing_landed(self, ledger):
        """Provider rejection yields nothing_landed with evidence."""
        intent = EffectIntent(
            effect_id=f"test-{uuid4().hex[:8]}",
            mission_id="test-mission",
            authority_id=f"auth-{uuid4().hex[:8]}",
            operation="email.send",
            target="invalid@@bad",
            payload_digest=hashlib.sha256(b"test").hexdigest(),
            idempotency_key=f"idem-{uuid4().hex[:8]}",
        )

        ledger.commit_intent(intent)
        ledger.record_dispatch(intent.effect_id)

        provider = SimulatedEmailProvider("invalid_address")
        result = ledger.reconcile(intent.effect_id, provider)

        assert result.finding == EffectStatus.NOTHING_LANDED
        assert result.evidence_reference
        assert "provider-rejection-log" in result.evidence_reference.record_id

        effect = ledger.get_effect(intent.effect_id)
        assert effect.status == EffectStatus.NOTHING_LANDED
        assert effect.mode == ExecutionMode.ACTIVE

    def test_accepted_yields_something_landed(self, ledger, sample_intent):
        """Successful delivery yields something_landed with evidence."""
        ledger.commit_intent(sample_intent)
        ledger.record_dispatch(sample_intent.effect_id)

        provider = SimulatedEmailProvider("accepted")
        provider.dispatch(sample_intent)  # Simulate dispatch
        result = ledger.reconcile(sample_intent.effect_id, provider)

        assert result.finding == EffectStatus.SOMETHING_LANDED
        assert result.evidence_reference
        assert "delivery-receipt" in result.evidence_reference.record_id

        effect = ledger.get_effect(sample_intent.effect_id)
        assert effect.status == EffectStatus.SOMETHING_LANDED
        assert effect.mode == ExecutionMode.ACTIVE

    def test_connection_loss_yields_indeterminate(self, ledger, sample_intent):
        """Lost confirmation yields indeterminate, not failure."""
        ledger.commit_intent(sample_intent)
        ledger.record_dispatch(sample_intent.effect_id)
        ledger.mark_indeterminate(sample_intent.effect_id, "connection lost")

        effect = ledger.get_effect(sample_intent.effect_id)
        assert effect.status == EffectStatus.INDETERMINATE
        assert effect.mode == ExecutionMode.FAIL_SAFE_PLAN_MODE

    def test_terminal_reconciliation_requires_evidence(self):
        """Terminal findings must have a structured evidence reference."""
        with pytest.raises(ValueError, match="requires an EvidenceReference"):
            ReconciliationResult(
                finding=EffectStatus.SOMETHING_LANDED,
                evidence_reference=None,
                explanation="test",
            )

    def test_evidence_fields_and_explanation_reject_whitespace(self, sample_intent):
        with pytest.raises(ValueError, match="source must be a non-blank string"):
            EvidenceReference(
                kind=EvidenceKind.ATTESTED,
                source="   ",
                record_id="provider-record",
                subject_idempotency_key=sample_intent.idempotency_key,
                artifact_digest=hashlib.sha256(b"record").hexdigest(),
            )

        with pytest.raises(ValueError, match="SHA-256 hex digest"):
            EvidenceReference(
                kind=EvidenceKind.ATTESTED,
                source="test-provider",
                record_id="provider-record",
                subject_idempotency_key=sample_intent.idempotency_key,
                artifact_digest="z" * 64,
            )

        with pytest.raises(ValueError, match="explanation must be a non-blank string"):
            ReconciliationResult(
                finding=EffectStatus.INDETERMINATE,
                evidence_reference=None,
                explanation="   ",
            )

    def test_reconciliation_before_dispatch_is_rejected_without_event(
        self, ledger, sample_intent
    ):
        ledger.commit_intent(sample_intent)
        reconciler = StaticReconciler(
            ReconciliationResult(
                finding=EffectStatus.SOMETHING_LANDED,
                evidence_reference=evidence_for(sample_intent, "pre-dispatch-record"),
                explanation="Would claim delivery.",
            )
        )

        with pytest.raises(InvalidStateTransitionError, match="Cannot reconcile"):
            ledger.reconcile(sample_intent.effect_id, reconciler)

        assert reconciler.calls == 0
        assert (
            ledger.get_effect(sample_intent.effect_id).status
            == EffectStatus.COMMITTED_NOT_DISPATCHED
        )
        assert [event.event_type for event in ledger.events_for(sample_intent.effect_id)] == [
            "intent_committed"
        ]

    @pytest.mark.parametrize(
        ("first_finding", "second_finding", "expected_disposition"),
        [
            (
                EffectStatus.SOMETHING_LANDED,
                EffectStatus.NOTHING_LANDED,
                AuthorityDisposition.CONSUMED,
            ),
            (
                EffectStatus.NOTHING_LANDED,
                EffectStatus.SOMETHING_LANDED,
                AuthorityDisposition.RELEASED,
            ),
        ],
    )
    def test_terminal_outcomes_are_absorbing(
        self,
        ledger,
        sample_intent,
        first_finding,
        second_finding,
        expected_disposition,
    ):
        ledger.commit_intent(sample_intent)
        ledger.record_dispatch(sample_intent.effect_id)
        first = StaticReconciler(
            ReconciliationResult(
                finding=first_finding,
                evidence_reference=evidence_for(sample_intent, "first-terminal-record"),
                explanation="First terminal result.",
            )
        )
        second = StaticReconciler(
            ReconciliationResult(
                finding=second_finding,
                evidence_reference=evidence_for(sample_intent, "second-terminal-record"),
                explanation="Conflicting terminal result.",
            )
        )

        ledger.reconcile(sample_intent.effect_id, first)
        settled_evidence = ledger.get_effect(sample_intent.effect_id).reconciliation_evidence

        with pytest.raises(InvalidStateTransitionError, match="Cannot reconcile"):
            ledger.reconcile(sample_intent.effect_id, second)

        effect = ledger.get_effect(sample_intent.effect_id)
        assert second.calls == 0
        assert effect.status == first_finding
        assert effect.reconciliation_evidence == settled_evidence
        assert ledger.get_authority(sample_intent.effect_id).disposition == expected_disposition

    def test_mismatched_evidence_subject_cannot_terminalize(self, ledger, sample_intent):
        ledger.commit_intent(sample_intent)
        ledger.record_dispatch(sample_intent.effect_id)
        reconciler = StaticReconciler(
            ReconciliationResult(
                finding=EffectStatus.SOMETHING_LANDED,
                evidence_reference=evidence_for(
                    sample_intent,
                    "wrong-subject-record",
                    subject_idempotency_key="different-effect-key",
                ),
                explanation="Evidence belongs to another effect.",
            )
        )

        with pytest.raises(ReconciliationError, match="does not match"):
            ledger.reconcile(sample_intent.effect_id, reconciler)

        assert (
            ledger.get_effect(sample_intent.effect_id).status
            == EffectStatus.DISPATCHED_UNCONFIRMED
        )
        assert (
            ledger.get_authority(sample_intent.effect_id).disposition
            == AuthorityDisposition.RESERVED
        )

    def test_indeterminate_result_enters_fail_safe_mode(self, ledger, sample_intent):
        ledger.commit_intent(sample_intent)
        ledger.record_dispatch(sample_intent.effect_id)
        reconciler = StaticReconciler(
            ReconciliationResult(
                finding=EffectStatus.INDETERMINATE,
                evidence_reference=evidence_for(sample_intent, "unavailable-record"),
                explanation="Provider outcome remains unavailable.",
            )
        )

        ledger.reconcile(sample_intent.effect_id, reconciler)

        effect = ledger.get_effect(sample_intent.effect_id)
        assert effect.status == EffectStatus.INDETERMINATE
        assert effect.mode == ExecutionMode.FAIL_SAFE_PLAN_MODE
        assert (
            ledger.get_authority(sample_intent.effect_id).disposition
            == AuthorityDisposition.HELD_UNRECONCILED
        )

    def test_competing_terminal_results_cannot_overwrite_winner(self, ledger, sample_intent):
        ledger.commit_intent(sample_intent)
        ledger.record_dispatch(sample_intent.effect_id)
        barrier = Barrier(2)
        reconcilers = [
            BarrierReconciler(
                ReconciliationResult(
                    finding=finding,
                    evidence_reference=evidence_for(sample_intent, f"concurrent-{finding.value}"),
                    explanation=f"Concurrent claim: {finding.value}.",
                ),
                barrier,
            )
            for finding in (EffectStatus.SOMETHING_LANDED, EffectStatus.NOTHING_LANDED)
        ]

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(ledger.reconcile, sample_intent.effect_id, reconciler)
                for reconciler in reconcilers
            ]
            outcomes = []
            for future in futures:
                try:
                    outcomes.append(future.result(timeout=10))
                except InvalidStateTransitionError as error:
                    outcomes.append(error)

        assert sum(isinstance(outcome, ReconciliationResult) for outcome in outcomes) == 1
        assert sum(isinstance(outcome, InvalidStateTransitionError) for outcome in outcomes) == 1
        effect = ledger.get_effect(sample_intent.effect_id)
        authority = ledger.get_authority(sample_intent.effect_id)
        expected_disposition = (
            AuthorityDisposition.CONSUMED
            if effect.status == EffectStatus.SOMETHING_LANDED
            else AuthorityDisposition.RELEASED
        )
        assert authority.disposition == expected_disposition
        terminal_events = [
            event for event in ledger.events_for(sample_intent.effect_id)
            if event.event_type in {"reconciled_something_landed", "reconciled_nothing_landed"}
        ]
        assert len(terminal_events) == 1


class TestFailSafePlanMode:
    """Test fail-safe plan mode behavior."""

    def test_indeterminate_enters_fail_safe_mode(self, ledger, sample_intent):
        """Indeterminate effect enters fail-safe plan mode."""
        ledger.commit_intent(sample_intent)
        ledger.record_dispatch(sample_intent.effect_id)
        ledger.mark_indeterminate(sample_intent.effect_id, "connection lost")

        effect = ledger.get_effect(sample_intent.effect_id)
        assert effect.mode == ExecutionMode.FAIL_SAFE_PLAN_MODE

    def test_fail_safe_mode_remains_until_reconciliation(self, ledger, sample_intent):
        """Fail-safe mode stays active until reconciliation resolves."""
        ledger.commit_intent(sample_intent)
        ledger.record_dispatch(sample_intent.effect_id)
        ledger.mark_indeterminate(sample_intent.effect_id, "connection lost")

        effect = ledger.get_effect(sample_intent.effect_id)
        assert effect.mode == ExecutionMode.FAIL_SAFE_PLAN_MODE

        # Reconcile
        provider = SimulatedEmailProvider("connection_loss_after_possible_acceptance")
        result = ledger.reconcile(sample_intent.effect_id, provider)

        effect = ledger.get_effect(sample_intent.effect_id)

        if result.finding == EffectStatus.INDETERMINATE:
            # Still in fail-safe mode
            assert effect.mode == ExecutionMode.FAIL_SAFE_PLAN_MODE
        else:
            # Resolved - back to active
            assert effect.mode == ExecutionMode.ACTIVE


class TestEventTimeline:
    """Test event recording and chronology."""

    def test_events_are_chronological(self, ledger, sample_intent):
        """Events must be in chronological order."""
        ledger.commit_intent(sample_intent)
        ledger.record_dispatch(sample_intent.effect_id)
        ledger.mark_indeterminate(sample_intent.effect_id, "test")

        events = ledger.events_for(sample_intent.effect_id)

        # Check sequence numbers
        for i, event in enumerate(events, start=1):
            assert event.sequence >= i

        # Check event types are in order
        event_types = [e.event_type for e in events]
        assert "intent_committed" in event_types[0]
        assert "dispatch_recorded" in event_types[1]
        assert "effect_indeterminate" in event_types[2]

    def test_events_include_evidence(self, ledger, sample_intent):
        """Events include evidence references where applicable."""
        ledger.commit_intent(sample_intent)
        ledger.record_dispatch(sample_intent.effect_id)

        provider = SimulatedEmailProvider("accepted")
        provider.dispatch(sample_intent)
        ledger.reconcile(sample_intent.effect_id, provider)

        events = ledger.events_for(sample_intent.effect_id)

        # Find reconciliation event
        recon_events = [e for e in events if "reconciled" in e.event_type]
        assert len(recon_events) > 0
        assert recon_events[0].evidence_reference is not None

    def test_illegal_transitions_leave_state_unchanged(self, ledger, sample_intent):
        """Illegal transitions don't corrupt ledger state."""
        ledger.commit_intent(sample_intent)

        # Try illegal indeterminate transition
        try:
            ledger.mark_indeterminate(sample_intent.effect_id, "illegal")
        except InvalidStateTransitionError:
            pass

        # Verify state unchanged
        effect = ledger.get_effect(sample_intent.effect_id)
        assert effect.status == EffectStatus.COMMITTED_NOT_DISPATCHED

        # Events should only show commit
        events = ledger.events_for(sample_intent.effect_id)
        assert len(events) == 1
        assert events[0].event_type == "intent_committed"

    def test_database_rejects_event_update_and_delete(self, ledger, sample_intent):
        ledger.commit_intent(sample_intent)
        original_events = ledger.events_for(sample_intent.effect_id)

        with sqlite3.connect(ledger.db_path) as conn:
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                conn.execute(
                    "UPDATE ledger_events SET event_type = 'tampered' WHERE effect_id = ?",
                    (sample_intent.effect_id,),
                )
            conn.rollback()
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                conn.execute(
                    "DELETE FROM ledger_events WHERE effect_id = ?",
                    (sample_intent.effect_id,),
                )

        assert ledger.events_for(sample_intent.effect_id) == original_events

    def test_ledger_connections_enforce_foreign_keys(self, ledger):
        with ledger._transaction() as conn:
            assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
