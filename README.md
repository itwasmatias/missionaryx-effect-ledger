# MissionaryX Effect Ledger

**Honest, governed execution for AI missions.**

> Read the companion technical essay: [Task Failure Is Not Effect Failure](docs/Task-Failure-Is-Not-Effect-Failure.md)

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

1. **`nothing_landed`** - Evidence supports that it did not execute
2. **`something_landed`** - Evidence supports that it did execute
3. **`indeterminate`** - Cannot prove either way (yet)

### Key Principles

1. **Intent before dispatch** - Effect intent is committed atomically before any dispatch attempt
2. **Authority reservation** - Each effect reserves specific authority that cannot be double-spent
3. **Honest indeterminacy** - Lost confirmation is modeled as `indeterminate`, not as failure
4. **Evidence-bound resolution** - Terminal findings require structured provenance bound to the effect
5. **Fail-safe mode** - Indeterminate effects enter fail-safe plan mode and cannot auto-retry
6. **Append-only timeline** - SQLite triggers reject event updates and deletions

### Evidence Trust Boundary

MissionaryX distinguishes three evidence kinds:

- **`enforced`** - A property guaranteed by a control at the system boundary
- **`attested`** - A claim made by a provider or other external authority
- **`observed`** - A record captured by an observer

A terminal reconciliation must carry an `EvidenceReference` with its kind, source,
record identifier, observation time, SHA-256 artifact digest, and the effect's exact
idempotency key. The ledger validates that structure and binding before changing
state or authority.

This standalone artifact does **not** authenticate a provider or independently prove
that an attestation is true. That is the reconciler's explicit trust boundary. A
production reconciler must authenticate its provider and verify or capture the
referenced artifact before returning a terminal result.

## Email Connection-Loss Example

```python
import hashlib

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
    payload_digest=hashlib.sha256(b"Welcome email body").hexdigest(),
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
# Evidence: structured, effect-bound provider attestation or observation
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
python -m missionaryx_ledger demo connection-loss
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

**`Reconciler` Protocol** - Evidence-based reconciliation trust boundary
- Queries provider state using idempotency keys
- Returns a finding with structured provenance and artifact binding
- Supports `nothing_landed`, `something_landed`, or `indeterminate`
- Remains responsible for authenticating the provider and evidence

**`SimulatedEmailProvider`** - Deterministic test provider
- Never makes network calls
- Simulates rejection, acceptance, and connection loss
- Demonstrates reconciliation patterns

### State Model

**Effect Status:**
- `committed_not_dispatched` - Intent recorded, not yet dispatched
- `dispatched_unconfirmed` - Dispatch recorded, awaiting outcome
- `nothing_landed` - Evidence supports non-execution (terminal)
- `something_landed` - Evidence supports execution (terminal)
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
**`ledger_events` table** - Append-only event timeline protected by update/delete triggers

All tables use explicit transactions. No state change occurs without a corresponding event record.
Foreign-key checks are enabled on every ledger connection. The triggers block ordinary
SQL mutation of events; they are not a cryptographic tamper-evidence mechanism and do
not protect against a database owner who can alter the schema.

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
✓ Reconciliation before dispatch is rejected without invoking the reconciler
✓ Terminal outcomes are absorbing and cannot reverse authority disposition
✓ Competing reconciliation results cannot overwrite the terminal winner
✓ Terminal reconciliation requires typed, effect-bound evidence provenance
✓ Blank, malformed, and mismatched evidence is rejected
✓ Event records are chronological and database-enforced append-only
✓ Illegal state transitions raise errors and leave state unchanged

## Non-Goals

This package is a **focused demonstration** of agent reliability principles. It deliberately does **not**:

- Send real emails or access live providers
- Store or transmit credentials
- Provide a complete agent framework
- Implement distributed consensus or multi-node coordination
- Guarantee legal admissibility of evidence
- Authenticate provider attestations in this simulated standalone package
- Claim exactly-once delivery (it models honest uncertainty)
- Provide production-grade encryption or access control

For production use, you would need:
- Durable storage with replication
- Cryptographic evidence signatures
- Provider-specific reconciliation implementations
- Access controls and audit logging
- Distributed transaction coordination
- Real provider integrations

The SQLite database contains operational metadata such as targets, idempotency keys,
timestamps, explanations, and evidence locators. Treat the database as potentially
sensitive even though raw message bodies and credentials are not stored.

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
3. Resolution requires structured evidence bound to the exact effect
4. Terminal outcomes cannot be reversed by stale or competing reconciliation
5. Event updates and deletions are rejected at the database boundary

The implementation prioritizes **conceptual clarity** over **production scale**. It uses SQLite, deterministic simulations, and focused scenarios to make the reliability mechanism visible.

## Contributing

This is a demonstration artifact. For questions or discussions about the reliability model, please open an issue describing your scenario.

## Acknowledgments

This package explores ideas from:
- Saga pattern (long-running transactions)
- Write-ahead logging (intent before execution)
- Idempotent operations (provider deduplication)
- Honest uncertainty (three-valued logic for real-world effects)
