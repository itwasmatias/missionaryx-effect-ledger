# MissionaryX Effect Ledger

**Honest, governed execution for AI missions.**

## The Problem

When an AI agent reports that its task failed, that is **not proof** that its real-world action failed.

Consider this scenario:

```
Agent: "Send email to user@example.com"
Worker: Dispatches email to provider
Network: Connection lost after request may have been accepted
Worker: Reports "Task failed"
```

**What actually happened?**

- The email might have been sent.
- The email might not have been sent.
- **The system doesn't know.**

Most systems treat this as a simple failure and retry the action. But retrying an action that may have already succeeded can lead to:

- Duplicate emails, charges, or operations
- Authority/quota double-spending
- Violated exactly-once semantics
- Incorrect audit trails

**The truth:** An agent task reporting failure does not prove the external effect failed. The effect status is **indeterminate** until reconciled with evidence.

## The Solution

The MissionaryX Effect Ledger treats task status and effect status as **separate facts**.

### Three Effect Outcomes

Every real-world action ends in one of three states:

1. **`nothing_landed`** - Provably did not execute (with evidence)
2. **`something_landed`** - Provably did execute (with evidence)
3. **`indeterminate`** - Cannot prove either way (yet)

### Key Principles

1. **Intent before dispatch** - Effect intent is committed atomically before any dispatch attempt
2. **Authority reservation** - Each effect reserves specific authority that cannot be double-spent
3. **Honest indeterminacy** - Lost confirmation is modeled as `indeterminate`, not as failure
4. **Evidence-based resolution** - Only verified evidence from providers can resolve indeterminate effects
5. **Fail-safe mode** - Indeterminate effects enter fail-safe plan mode and cannot auto-retry
6. **Append-only timeline** - All state changes are recorded as immutable events

## Email Connection-Loss Example

```python
from missionaryx_ledger import EffectLedger, EffectIntent
from missionaryx_ledger.simulated_provider import SimulatedEmailProvider

# Create ledger
ledger = EffectLedger("effects.db")

# Commit intent BEFORE dispatch
intent = EffectIntent(
    effect_id="send-welcome-email-001",
    mission_id="onboarding-mission",
    authority_id="email-quota-batch-5",
    operation="email.send",
    target="user@example.com",
    payload_digest="a7f3...",  # SHA-256, never raw content
    idempotency_key="welcome-email-user-123",
)
ledger.commit_intent(intent)

# Authority is now reserved - cannot be reused
# Effect status: committed_not_dispatched

# Record dispatch
ledger.record_dispatch(intent.effect_id)
# Effect status: dispatched_unconfirmed

# Simulate dispatch with connection loss
provider = SimulatedEmailProvider("connection_loss_after_possible_acceptance")
try:
    provider.dispatch(intent)
except ConnectionError:
    # Connection lost - effect status is unknown
    ledger.mark_indeterminate(intent.effect_id, "Connection lost after possible acceptance")
    # Effect status: indeterminate
    # Execution mode: fail_safe_plan_mode
    # Authority disposition: held_unreconciled

# Attempting to reuse authority now raises AuthorityAlreadyUsedError
# The effect CANNOT be silently retried

# Later: reconcile with provider evidence
result = ledger.reconcile(intent.effect_id, provider)
# Finding: something_landed OR indeterminate
# Evidence: provider delivery logs, receipts, or unavailability notice
# Authority: consumed (if landed) or still held (if still indeterminate)
```

## Installation

```bash
cd missionaryx-effect-ledger
pip install -e .
```

For development:

```bash
pip install -e .[dev]
```

## Testing

Run the test suite:

```bash
pytest
```

Run with verbose output:

```bash
pytest -v
```

## Demonstrations

The package includes three demonstration scenarios:

###  Connection Loss (The Centerpiece)

```bash
missionaryx-ledger demo connection-loss
```

or

```bash
python -m missionaryx_ledger.demo connection-loss
```

This demonstrates:
- Intent committed before dispatch
- Authority reserved and tracked
- Connection lost after possible acceptance
- Effect status becomes `indeterminate`
- Execution mode becomes `fail_safe_plan_mode`
- Authority held as `held_unreconciled`
- Attempted authority reuse correctly rejected
- Reconciliation with eventual evidence
- Final resolution or continued indeterminacy

### Invalid Address

```bash
missionaryx-ledger demo invalid-address
```

Demonstrates immediate provider rejection → `nothing_landed` with evidence.

### Accepted

```bash
missionaryx-ledger demo accepted
```

Demonstrates successful delivery → `something_landed` with evidence.

## Architecture

### Core Components

**`EffectLedger`** - SQLite-backed transactional ledger
- Atomic intent commitment
- State transition enforcement
- Event timeline recording
- Authority reservation tracking

**`EffectIntent`** - Immutable effect specification
- Committed before any dispatch attempt
- Contains operation, target, payload digest, idempotency key
- Never stores raw sensitive payloads

**`Reconciler` Protocol** - Evidence-based reconciliation
- Queries provider state using idempotency keys
- Returns finding with evidence reference
- Supports `nothing_landed`, `something_landed`, or `indeterminate`

**`SimulatedEmailProvider`** - Deterministic test provider
- Never makes network calls
- Simulates rejection, acceptance, and connection loss
- Demonstrates reconciliation patterns

### State Model

**Effect Status:**
- `committed_not_dispatched` - Intent recorded, not yet dispatched
- `dispatched_unconfirmed` - Dispatch recorded, awaiting outcome
- `nothing_landed` - Provably did not execute (terminal, with evidence)
- `something_landed` - Provably did execute (terminal, with evidence)
- `indeterminate` - Cannot prove outcome (may resolve later)

**Execution Mode:**
- `active` - Normal operation
- `fail_safe_plan_mode` - Indeterminate effect, no auto-retry allowed

**Authority Disposition:**
- `reserved` - Held for pending effect
- `consumed` - Used by executed effect
- `released` - Freed after proven non-execution
- `held_unreconciled` - Locked due to indeterminate state

### Database Schema

**`effects` table** - Effect intents and current state
**`authority_reservations` table** - Authority lifecycle tracking
**`ledger_events` table** - Append-only event timeline

All tables use explicit transactions. No state change occurs without a corresponding event record.

## Reliability Invariants (Proven by Tests)

✓ Intent is committed before dispatch is allowed
✓ Dispatch cannot be recorded twice
✓ Same idempotency key cannot create two effects
✓ Same authority cannot create two effects
✓ Invalid-address reconciliation → `nothing_landed` + releases authority
✓ Accepted reconciliation → `something_landed` + consumes authority
✓ Lost confirmation after dispatch → `indeterminate` (not generic failure)
✓ Indeterminate effect cannot be dispatched/retried again
✓ Indeterminate authority cannot be reused or released by timeout
✓ Fail-safe plan mode remains until reconciliation resolves
✓ Terminal reconciliation requires non-empty evidence reference
✓ Event records are chronological and append-only
✓ Illegal state transitions raise errors and leave state unchanged

## Non-Goals

This package is a **focused demonstration** of agent reliability principles. It deliberately does **not**:

- Send real emails or access live providers
- Store or transmit credentials
- Provide a complete agent framework
- Implement distributed consensus or multi-node coordination
- Guarantee legal admissibility of evidence
- Claim exactly-once delivery (it models honest uncertainty)
- Provide production-grade encryption or access control

For production use, you would need:
- Durable storage with replication
- Cryptographic evidence signatures
- Provider-specific reconciliation implementations
- Access controls and audit logging
- Distributed transaction coordination
- Real provider integrations

## CLI Reference

### Run demonstrations

```bash
missionaryx-ledger demo [scenario]
```

Scenarios: `connection-loss`, `invalid-address`, `accepted`

### Show event timeline

```bash
missionaryx-ledger events <effect-id> --db <database-path>
```

## License

MIT License - See LICENSE file for details.

## For Researchers

This package demonstrates a specific position on AI agent reliability:

**Claim:** Task failure and effect failure are distinct. Systems must model indeterminacy honestly and prevent unsafe retries.

**Evidence:** The test suite proves that:
1. Authority cannot be double-spent during indeterminacy
2. Indeterminate effects cannot auto-retry
3. Resolution requires explicit evidence
4. All transitions are auditable

The implementation prioritizes **conceptual clarity** over **production scale**. It uses SQLite, deterministic simulations, and focused scenarios to make the reliability mechanism visible.

## Contributing

This is a demonstration artifact. For questions or discussions about the reliability model, please open an issue describing your scenario.

## Acknowledgments

This package explores ideas from:
- Saga pattern (long-running transactions)
- Write-ahead logging (intent before execution)
- Idempotent operations (provider deduplication)
- Honest uncertainty (three-valued logic for real-world effects)
