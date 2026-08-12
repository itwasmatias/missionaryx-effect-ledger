"""Demonstration scenarios for the effect ledger."""

import hashlib
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from missionaryx_ledger.errors import AuthorityAlreadyUsedError, DispatchNotAllowedError
from missionaryx_ledger.ledger import EffectLedger
from missionaryx_ledger.models import EffectIntent, EffectStatus
from missionaryx_ledger.simulated_provider import SimulatedEmailProvider, ScenarioType


def run_scenario(scenario: ScenarioType, verbose: bool = True) -> None:
    """Run a demonstration scenario.

    Args:
        scenario: Which scenario to run
        verbose: Whether to print detailed output
    """
    # Create temporary database
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        ledger = EffectLedger(db_path)
        provider = SimulatedEmailProvider(scenario)

        if verbose:
            print(f"\n{'='*70}")
            print(f"MissionaryX Effect Ledger Demo: {scenario}")
            print(f"{'='*70}\n")

        # Create effect intent
        effect_id = f"effect-{scenario}-{uuid4().hex[:8]}"
        authority_id = f"auth-{scenario}-{uuid4().hex[:8]}"
        idempotency_key = f"idem-{scenario}-{uuid4().hex[:8]}"
        target = "test@example.com" if scenario != "invalid_address" else "invalid@@bad"

        payload_digest = hashlib.sha256(f"Test email content for {scenario}".encode()).hexdigest()

        intent = EffectIntent(
            effect_id=effect_id,
            mission_id="demo-mission",
            authority_id=authority_id,
            operation="email.send",
            target=target,
            payload_digest=payload_digest,
            idempotency_key=idempotency_key,
        )

        # Commit intent
        if verbose:
            print("1. Committing effect intent...")
        ledger.commit_intent(intent)

        effect = ledger.get_effect(effect_id)
        auth = ledger.get_authority(effect_id)

        if verbose:
            print(f"   ✓ Intent committed: {effect_id}")
            print(f"   ✓ Effect status: {effect.status.value}")
            print(f"   ✓ Authority reserved: {auth.disposition.value}\n")

        # Record dispatch
        if verbose:
            print("2. Recording dispatch...")
        ledger.record_dispatch(effect_id)

        effect = ledger.get_effect(effect_id)
        if verbose:
            print(f"   ✓ Dispatch recorded")
            print(f"   ✓ Effect status: {effect.status.value}\n")

        # Attempt dispatch with provider
        if verbose:
            print("3. Dispatching to provider...")

        dispatch_succeeded = False
        connection_lost = False

        try:
            success, error = provider.dispatch(intent)
            dispatch_succeeded = success
            if verbose:
                if success:
                    print(f"   ✓ Provider accepted dispatch")
                else:
                    print(f"   ✗ Provider rejected: {error}")
        except ConnectionError as e:
            connection_lost = True
            if verbose:
                print(f"   ✗ CONNECTION LOST: {e}")

        if verbose:
            print()

        # Handle connection loss
        if connection_lost or scenario == "connection_loss_after_possible_acceptance":
            if verbose:
                print("4. Connection lost - effect status now INDETERMINATE")
            ledger.mark_indeterminate(effect_id, "Connection lost after possible acceptance")

            effect = ledger.get_effect(effect_id)
            auth = ledger.get_authority(effect_id)

            if verbose:
                print(f"   ✓ Effect status: {effect.status.value}")
                print(f"   ✓ Execution mode: {effect.mode.value}")
                print(f"   ✓ Authority disposition: {auth.disposition.value}\n")

            # Try to reuse authority (should fail)
            if verbose:
                print("5. Attempting to reuse authority (should fail)...")

            duplicate_intent = EffectIntent(
                effect_id=f"duplicate-{uuid4().hex[:8]}",
                mission_id="demo-mission",
                authority_id=authority_id,  # Same authority!
                operation="email.send",
                target="another@example.com",
                payload_digest=hashlib.sha256(b"different content").hexdigest(),
                idempotency_key=f"different-{uuid4().hex[:8]}",
            )

            try:
                ledger.commit_intent(duplicate_intent)
                if verbose:
                    print("   ✗ ERROR: Should have rejected duplicate authority!")
            except AuthorityAlreadyUsedError as e:
                if verbose:
                    print(f"   ✓ Correctly rejected: {e}\n")

        # Reconcile
        if verbose:
            print(f"{'6' if connection_lost else '4'}. Reconciling with provider...")

        result = ledger.reconcile(effect_id, provider)

        effect = ledger.get_effect(effect_id)
        auth = ledger.get_authority(effect_id)

        if verbose:
            print(f"   ✓ Reconciliation finding: {result.finding.value}")
            print(f"   ✓ Evidence: {result.evidence_reference}")
            print(f"   ✓ Explanation: {result.explanation}")
            print(f"   ✓ Final effect status: {effect.status.value}")
            print(f"   ✓ Final authority disposition: {auth.disposition.value}\n")

        # Show event timeline
        if verbose:
            print(f"{'7' if connection_lost else '5'}. Event timeline:")
            events = ledger.events_for(effect_id)
            for event in events:
                print(f"   [{event.sequence}] {event.event_type}")
                if event.evidence_reference:
                    print(f"       Evidence: {event.evidence_reference}")
                if event.metadata:
                    print(f"       Metadata: {event.metadata}")

            print(f"\n{'='*70}\n")

    finally:
        # Cleanup
        Path(db_path).unlink(missing_ok=True)


def demo_connection_loss():
    """Demonstrate the connection-loss scenario (the centerpiece)."""
    run_scenario("connection_loss_after_possible_acceptance")


def demo_invalid_address():
    """Demonstrate immediate rejection scenario."""
    run_scenario("invalid_address")


def demo_accepted():
    """Demonstrate successful delivery scenario."""
    run_scenario("accepted")
