# Task Failure Is Not Effect Failure

*Why reliable AI agents need effect state, evidence boundaries, and fail-safe plan mode*

**Matias Rodriguez**

AI agents are beginning to act outside the chat window. They can send emails, create records, start deployments, call tools, and make changes on a person's behalf.

That makes one question unusually important: when an agent says an action failed, what actually happened in the outside world?

Imagine an agent sends an important email. The request leaves the system, but the connection dies before the email provider returns confirmation. The worker reports an error. From the worker's point of view, the task failed. But the email may already have been accepted.

The system cannot honestly say the email was sent. It also cannot honestly say it was not sent. The concrete fact that matters to the user is this: the action was attempted, but there is not yet enough evidence to determine its real-world effect.

That is different from an email provider immediately rejecting an incorrect address. In that case, the provider returned evidence that the email was not accepted. The user should be told that nothing was sent and asked for the correct address.

Both cases may produce a failed task. They do not produce the same effect.

## Two Statuses, Not One

A task status describes what happened inside the agent system: whether a worker completed, timed out, crashed, or lost its connection.

An effect status describes what happened outside the agent system: whether an email was accepted, a payment was created, a deployment started, or a record changed.

Those facts are related, but one cannot safely substitute for the other.

A task can fail while its effect succeeds. A task can also fail while its effect remains unknown. Treating both cases as simply `failed` hides the information that matters most to the user: what may have changed outside the system.

This leads to a three-valued effect model:

- **Nothing landed:** Evidence supports that the outside system rejected or did not execute the action.
- **Something landed:** Evidence supports that the outside system accepted or executed the action.
- **Indeterminate:** The action was attempted, but available evidence cannot prove either outcome.

`Indeterminate` is not a softer spelling of failure. It is a first-class state with different safety consequences.

## Why Automatic Retry Is Dangerous

If the agent converts a lost confirmation into `failed`, a normal retry policy may run the action again. That can send the same email twice. The same pattern can duplicate a payment, repeat a deployment, create two support tickets, or apply the same account change more than once.

An idempotency key can help when an external provider supports it correctly, but the agent should not assume that every operation, provider, or failure mode gives exactly-once behavior. The system still needs an internal account of the effect it intended, the authority it exercised, and the evidence it received.

The safe default is simple: an unconfirmed effect must not silently retry.

## Record Intent Before Dispatch

Before an important request escapes, the system should durably record its intended effect. I think of this as the agent's black box.

The record should identify:

- the operation;
- the target;
- the authority granted for that action;
- a digest of the payload rather than unnecessary raw content;
- an idempotency key;
- and the time the intent was created.

This record does not prove that the external action happened. It proves what the agent was authorized and prepared to attempt.

That distinction matters. If the worker crashes after dispatch, the original intent survives. The system has enough information to investigate the external effect instead of reconstructing the action from an error message or conversational memory.

Intent commitment must precede dispatch. Otherwise the system can create the most dangerous kind of uncertainty: an external action may have occurred without a durable internal record of what was attempted.

## Authority Follows the Effect

Permission to perform a consequential action should be reserved for that specific effect before dispatch.

If confirmation is lost, the permission must remain held. Releasing it because the worker failed would allow another attempt to spend the same authority while the first effect is still unresolved.

The authority lifecycle follows effect status:

- `something_landed` consumes the authority;
- `nothing_landed` releases it;
- `indeterminate` holds it unreconciled.

Authority follows the effect status—not the agent's error message.

This is more than bookkeeping. It makes the safety policy enforceable. The agent cannot merely promise not to retry; it lacks reusable authority while the original outcome remains unknown.

## Evidence Is Not One Thing

Calling all records “evidence” can create another false sense of certainty. A trustworthy ledger should preserve how a claim is grounded.

I use three categories:

- **Enforced evidence** records something a control actually guaranteed. For example, the ledger committed intent before dispatch, reserved one authority identifier for one effect, or rejected an illegal state transition.
- **Attested evidence** is a claim made by an external authority, such as an email provider's rejection record or delivery receipt.
- **Observed evidence** records what an observer saw, such as a network disconnect or an unavailable provider response.

These are not interchangeable. Observing a connection failure does not prove that an email was rejected. A provider attestation is stronger evidence about the provider's state, but it still depends on authenticating and trusting that provider. An enforced ledger invariant proves what the ledger controlled, not what happened in someone else's infrastructure.

For that reason, terminal evidence should be structured and bound to the exact effect. In the MissionaryX Effect Ledger, an evidence reference includes its kind, source, record identifier, observation time, artifact digest, and the effect's idempotency key.

The ledger can validate the structure and binding. It cannot manufacture external truth. Provider authentication and verification remain an explicit reconciler trust boundary.

That limitation should be documented, not hidden behind the word “verified.” Honesty includes saying what the evidence can and cannot establish.

## Reconciliation Is a State Transition

When an effect is unresolved, the system queries the relevant provider or evidence source using the saved intent.

If the provider returns effect-bound evidence of rejection, the ledger can move to `nothing_landed` and release the authority. If it returns effect-bound evidence of acceptance or execution, the ledger can move to `something_landed` and consume the authority.

If the provider still cannot establish an outcome, the effect remains `indeterminate`.

Reconciliation must obey the same lifecycle rules as dispatch. It cannot occur before an effect was dispatched. Once an effect reaches an accepted terminal state, ordinary reconciliation must not reverse it. Otherwise a delayed or competing result could change `something_landed` into `nothing_landed` and release authority that had already been consumed.

Concurrency makes this more than a theoretical concern. Two reconciliation attempts may query a provider at the same time and return different results. The ledger must re-read state under a write lock and conditionally apply the result. One terminal result may win; the stale competitor must be rejected without rewriting effect state, evidence, or authority.

Terminal means terminal in the automated path.

## Fail-Safe Plan Mode

`EffectStatus` describes what is known about the outside world. A separate `ExecutionMode` describes what the system is allowed to do next.

When an effect is indeterminate, MissionaryX enters `fail_safe_plan_mode`. The agent may gather evidence, explain the situation, and prepare options. It may not silently dispatch another external action.

The system should tell the user:

- what was attempted;
- what evidence exists;
- what remains unknown;
- why retrying could create a duplicate effect;
- and which safe choices remain available.

If uncertainty persists, the effect stays visibly unresolved. The user—not an assumption hidden inside the agent—decides the next step.

## Reliability Requires Honesty and Integrity

An agent is not reliable merely because it can perform an action. Ability without honesty can still harm the user.

Honesty means giving the user the hard truth, not a partial truth chosen because it sounds better, completes the task faster, or makes the agent appear more capable. Integrity means preserving that truth in system behavior: holding authority, refusing an unsafe retry, recording evidence provenance, and preventing stale results from rewriting history.

The goal is not to eliminate every failure. Distributed systems will still lose connections, workers will still crash, and providers will still become unavailable.

The goal is to keep those failures from becoming ungoverned real-world effects.

## A Runnable Reference Implementation

I built the [MissionaryX Effect Ledger](https://github.com/itwasmatias/missionaryx-effect-ledger) as a small, runnable reference implementation of this position.

It is an offline Python and SQLite artifact, not a production agent framework. It demonstrates intent-before-dispatch, authority reservation, three-valued effect state, fail-safe plan mode, structured evidence provenance, absorbing terminal outcomes, concurrency-safe reconciliation, and database-enforced append-only events.

The repository includes deterministic email-provider simulations and 30 tests. Its CI runs on Python 3.11 and 3.12. It deliberately makes no live network calls, stores no credentials, and does not claim exactly-once delivery, cryptographic tamper evidence, or authenticated provider truth.

Those limitations are part of the argument. Reliable systems should make the boundary between what they enforce, what others attest, what they observe, and what remains unknown impossible to miss.

An agent failure is not proof that its action failed. Until evidence establishes the external effect, the only reliable answer may be: **we do not know yet, so we will not act as though we do.**
