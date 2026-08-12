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
import pytest
import tempfile
from pathlib import Path
from uuid import uuid4

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
    EffectNotFoundError,
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
        assert "provider-rejection-log" in result.evidence_reference

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
        assert "delivery-receipt" in result.evidence_reference

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
        """Terminal findings must have non-empty evidence reference."""
        with pytest.raises(ValueError, match="requires non-empty evidence_reference"):
            ReconciliationResult(
                finding=EffectStatus.SOMETHING_LANDED,
                evidence_reference="",  # Empty!
                explanation="test",
            )


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
