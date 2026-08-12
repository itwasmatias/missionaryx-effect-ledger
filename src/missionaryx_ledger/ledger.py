"""SQLite-backed effect ledger with transactional state management."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from missionaryx_ledger.errors import (
    AuthorityAlreadyUsedError,
    DispatchNotAllowedError,
    EffectNotFoundError,
    IdempotencyKeyConflictError,
    InvalidStateTransitionError,
    ReconciliationError,
)
from missionaryx_ledger.models import (
    AuthorityDisposition,
    AuthorityReservation,
    EvidenceReference,
    Effect,
    EffectIntent,
    EffectStatus,
    ExecutionMode,
    LedgerEvent,
)
from missionaryx_ledger.reconciliation import Reconciler, ReconciliationResult


class EffectLedger:
    """Durable ledger for effect intents, state, and evidence timeline.

    Uses SQLite with explicit transactions to ensure atomic state changes.
    All events are append-only. Authority reservations prevent double-spending.
    """

    def __init__(self, db_path: str | Path):
        """Initialize the ledger with a SQLite database.

        Args:
            db_path: Path to SQLite database file
        """
        self.db_path = Path(db_path)
        self._init_database()

    def _init_database(self) -> None:
        """Initialize database schema."""
        with self._transaction() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS effects (
                    effect_id TEXT PRIMARY KEY,
                    mission_id TEXT NOT NULL,
                    authority_id TEXT NOT NULL UNIQUE,
                    operation TEXT NOT NULL,
                    target TEXT NOT NULL,
                    payload_digest TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    dispatched_at TEXT,
                    resolved_at TEXT,
                    reconciliation_evidence TEXT
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS authority_reservations (
                    authority_id TEXT PRIMARY KEY,
                    effect_id TEXT NOT NULL,
                    disposition TEXT NOT NULL,
                    reserved_at TEXT NOT NULL,
                    disposition_changed_at TEXT,
                    FOREIGN KEY (effect_id) REFERENCES effects(effect_id)
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS ledger_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    effect_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    evidence_reference TEXT,
                    metadata_json TEXT,
                    FOREIGN KEY (effect_id) REFERENCES effects(effect_id)
                )
            """)

            # Indices for performance
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_effects_status ON effects(status)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_events_effect_id ON ledger_events(effect_id)
            """)

            # Enforce the append-only contract at the database boundary. These
            # triggers block ordinary UPDATE/DELETE statements, including ones
            # issued outside the public Python API.
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS ledger_events_reject_update
                BEFORE UPDATE ON ledger_events
                BEGIN
                    SELECT RAISE(ABORT, 'ledger_events is append-only');
                END
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS ledger_events_reject_delete
                BEFORE DELETE ON ledger_events
                BEGIN
                    SELECT RAISE(ABORT, 'ledger_events is append-only');
                END
            """)

    @contextmanager
    def _transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """Context manager for atomic transactions."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            if immediate:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _append_event(
        self,
        conn: sqlite3.Connection,
        effect_id: str,
        event_type: str,
        evidence_reference: EvidenceReference | None = None,
        metadata: dict | None = None,
    ) -> None:
        """Append an event to the timeline (within a transaction)."""
        timestamp = datetime.now(timezone.utc).isoformat()
        metadata_json = json.dumps(metadata) if metadata else None
        evidence_json = evidence_reference.to_json() if evidence_reference else None

        conn.execute(
            """
            INSERT INTO ledger_events (
                effect_id, event_type, timestamp, evidence_reference, metadata_json
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (effect_id, event_type, timestamp, evidence_json, metadata_json),
        )

    def commit_intent(self, intent: EffectIntent) -> None:
        """Commit an effect intent atomically.

        This is the only operation that creates an effect. It atomically:
        - Validates the intent
        - Creates the authority reservation
        - Enforces no duplicate authority ID
        - Enforces no duplicate idempotency key
        - Persists effect as committed_not_dispatched
        - Appends intent_committed event

        Args:
            intent: The effect intent to commit

        Raises:
            AuthorityAlreadyUsedError: If authority_id already used
            IdempotencyKeyConflictError: If idempotency_key already used
            ValueError: If intent validation fails
        """
        with self._transaction() as conn:
            # Check for duplicate authority
            existing_auth = conn.execute(
                "SELECT effect_id FROM authority_reservations WHERE authority_id = ?",
                (intent.authority_id,),
            ).fetchone()
            if existing_auth:
                raise AuthorityAlreadyUsedError(intent.authority_id, existing_auth["effect_id"])

            # Check for duplicate idempotency key
            existing_idem = conn.execute(
                "SELECT effect_id FROM effects WHERE idempotency_key = ?",
                (intent.idempotency_key,),
            ).fetchone()
            if existing_idem:
                raise IdempotencyKeyConflictError(
                    intent.idempotency_key, existing_idem["effect_id"]
                )

            # Insert effect
            conn.execute(
                """
                INSERT INTO effects (
                    effect_id, mission_id, authority_id, operation, target,
                    payload_digest, idempotency_key, status, mode, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    intent.effect_id,
                    intent.mission_id,
                    intent.authority_id,
                    intent.operation,
                    intent.target,
                    intent.payload_digest,
                    intent.idempotency_key,
                    EffectStatus.COMMITTED_NOT_DISPATCHED.value,
                    ExecutionMode.ACTIVE.value,
                    intent.created_at.isoformat(),
                ),
            )

            # Create authority reservation
            conn.execute(
                """
                INSERT INTO authority_reservations (
                    authority_id, effect_id, disposition, reserved_at
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    intent.authority_id,
                    intent.effect_id,
                    AuthorityDisposition.RESERVED.value,
                    intent.created_at.isoformat(),
                ),
            )

            # Append event
            self._append_event(conn, intent.effect_id, "intent_committed")

    def record_dispatch(self, effect_id: str) -> None:
        """Record that an effect has been dispatched.

        Only legal from committed_not_dispatched status.

        Args:
            effect_id: ID of the effect to mark as dispatched

        Raises:
            EffectNotFoundError: If effect doesn't exist
            InvalidStateTransitionError: If not in committed_not_dispatched state
        """
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT status FROM effects WHERE effect_id = ?", (effect_id,)
            ).fetchone()

            if not row:
                raise EffectNotFoundError(effect_id)

            current_status = EffectStatus(row["status"])
            if current_status != EffectStatus.COMMITTED_NOT_DISPATCHED:
                raise InvalidStateTransitionError(
                    f"Cannot dispatch from status {current_status.value}",
                    effect_id=effect_id,
                )

            # Update to dispatched
            dispatched_at = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """
                UPDATE effects
                SET status = ?, dispatched_at = ?
                WHERE effect_id = ?
                """,
                (EffectStatus.DISPATCHED_UNCONFIRMED.value, dispatched_at, effect_id),
            )

            # Append event
            self._append_event(conn, effect_id, "dispatch_recorded")

    def mark_indeterminate(self, effect_id: str, reason: str) -> None:
        """Mark an effect as indeterminate and enter fail-safe plan mode.

        Only legal after dispatch. Sets effect status to indeterminate,
        execution mode to fail_safe_plan_mode, and authority to held_unreconciled.

        Args:
            effect_id: ID of the effect
            reason: Human-readable reason for indeterminacy

        Raises:
            EffectNotFoundError: If effect doesn't exist
            InvalidStateTransitionError: If not dispatched
        """
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason must be a non-blank string")

        with self._transaction() as conn:
            row = conn.execute(
                "SELECT status FROM effects WHERE effect_id = ?", (effect_id,)
            ).fetchone()

            if not row:
                raise EffectNotFoundError(effect_id)

            current_status = EffectStatus(row["status"])
            if current_status != EffectStatus.DISPATCHED_UNCONFIRMED:
                raise InvalidStateTransitionError(
                    f"Cannot mark indeterminate from status {current_status.value}",
                    effect_id=effect_id,
                )

            # Update effect
            conn.execute(
                """
                UPDATE effects
                SET status = ?, mode = ?
                WHERE effect_id = ?
                """,
                (
                    EffectStatus.INDETERMINATE.value,
                    ExecutionMode.FAIL_SAFE_PLAN_MODE.value,
                    effect_id,
                ),
            )

            # Update authority
            changed_at = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """
                UPDATE authority_reservations
                SET disposition = ?, disposition_changed_at = ?
                WHERE effect_id = ?
                """,
                (AuthorityDisposition.HELD_UNRECONCILED.value, changed_at, effect_id),
            )

            # Append events
            self._append_event(conn, effect_id, "effect_indeterminate", metadata={"reason": reason})
            self._append_event(conn, effect_id, "fail_safe_plan_mode_entered")

    def reconcile(self, effect_id: str, reconciler: Reconciler) -> ReconciliationResult:
        """Reconcile a dispatched effect using structured evidence.

        Args:
            effect_id: ID of the effect to reconcile
            reconciler: Reconciler implementation

        Returns:
            ReconciliationResult with finding and evidence

        Raises:
            EffectNotFoundError: If effect doesn't exist
            ReconciliationError: If reconciliation fails
        """
        # First get the intent and reject illegal lifecycle states before
        # recording an attempt or invoking an external reconciler.
        intent = self._get_intent(effect_id)

        with self._transaction() as conn:
            self._require_reconcilable_status(conn, effect_id)
            self._append_event(conn, effect_id, "reconciliation_started")

        # Call reconciler (may be slow, outside transaction)
        try:
            result = reconciler.reconcile(intent)
        except Exception as e:
            # Record only the exception type: provider messages can contain
            # credentials or personal data.
            with self._transaction() as conn:
                self._append_event(
                    conn,
                    effect_id,
                    "reconciliation_failed",
                    metadata={"error_type": type(e).__name__},
                )
            raise ReconciliationError(
                f"Reconciliation failed with {type(e).__name__}", effect_id=effect_id
            ) from e

        if not isinstance(result, ReconciliationResult):
            raise ReconciliationError(
                "Reconciler must return a ReconciliationResult", effect_id=effect_id
            )

        evidence = result.evidence_reference
        if evidence is not None and evidence.subject_idempotency_key != intent.idempotency_key:
            raise ReconciliationError(
                "Evidence subject does not match the effect idempotency key",
                effect_id=effect_id,
            )

        # Re-read under a write lock and apply with a status guard. A competing
        # reconciliation that reached a terminal state first makes this result
        # stale and therefore ineligible to mutate state or authority.
        with self._transaction(immediate=True) as conn:
            self._require_reconcilable_status(conn, effect_id)
            if result.finding == EffectStatus.NOTHING_LANDED:
                self._apply_nothing_landed(conn, effect_id, result)
            elif result.finding == EffectStatus.SOMETHING_LANDED:
                self._apply_something_landed(conn, effect_id, result)
            else:  # Still indeterminate
                self._apply_still_indeterminate(conn, effect_id, result)

        return result

    @staticmethod
    def _require_reconcilable_status(
        conn: sqlite3.Connection, effect_id: str
    ) -> EffectStatus:
        """Return current status or reject a pre-dispatch/terminal reconciliation."""
        row = conn.execute(
            "SELECT status FROM effects WHERE effect_id = ?", (effect_id,)
        ).fetchone()
        if not row:
            raise EffectNotFoundError(effect_id)

        status = EffectStatus(row["status"])
        allowed = {
            EffectStatus.DISPATCHED_UNCONFIRMED,
            EffectStatus.INDETERMINATE,
        }
        if status not in allowed:
            raise InvalidStateTransitionError(
                f"Cannot reconcile from status {status.value}", effect_id=effect_id
            )
        return status

    @staticmethod
    def _update_authority_for_resolution(
        conn: sqlite3.Connection,
        effect_id: str,
        disposition: AuthorityDisposition,
        changed_at: str,
    ) -> None:
        """Atomically move pending authority to its terminal disposition."""
        cursor = conn.execute(
            """
            UPDATE authority_reservations
            SET disposition = ?, disposition_changed_at = ?
            WHERE effect_id = ? AND disposition IN (?, ?)
            """,
            (
                disposition.value,
                changed_at,
                effect_id,
                AuthorityDisposition.RESERVED.value,
                AuthorityDisposition.HELD_UNRECONCILED.value,
            ),
        )
        if cursor.rowcount != 1:
            raise ReconciliationError(
                "Authority is not pending reconciliation", effect_id=effect_id
            )

    def _apply_nothing_landed(
        self, conn: sqlite3.Connection, effect_id: str, result: ReconciliationResult
    ) -> None:
        """Apply nothing_landed reconciliation result."""
        resolved_at = datetime.now(timezone.utc).isoformat()
        evidence = result.evidence_reference
        if evidence is None:  # Guard for callers bypassing dataclass validation.
            raise ReconciliationError("Terminal result requires evidence", effect_id=effect_id)

        cursor = conn.execute(
            """
            UPDATE effects
            SET status = ?, mode = ?, resolved_at = ?, reconciliation_evidence = ?
            WHERE effect_id = ? AND status IN (?, ?)
            """,
            (
                EffectStatus.NOTHING_LANDED.value,
                ExecutionMode.ACTIVE.value,
                resolved_at,
                evidence.to_json(),
                effect_id,
                EffectStatus.DISPATCHED_UNCONFIRMED.value,
                EffectStatus.INDETERMINATE.value,
            ),
        )
        if cursor.rowcount != 1:
            raise InvalidStateTransitionError(
                "Reconciliation result became stale", effect_id=effect_id
            )

        # Release authority
        self._update_authority_for_resolution(
            conn,
            effect_id,
            AuthorityDisposition.RELEASED,
            datetime.now(timezone.utc).isoformat(),
        )

        self._append_event(
            conn,
            effect_id,
            "reconciled_nothing_landed",
            evidence_reference=evidence,
            metadata={"explanation": result.explanation},
        )

    def _apply_something_landed(
        self, conn: sqlite3.Connection, effect_id: str, result: ReconciliationResult
    ) -> None:
        """Apply something_landed reconciliation result."""
        resolved_at = datetime.now(timezone.utc).isoformat()
        evidence = result.evidence_reference
        if evidence is None:  # Guard for callers bypassing dataclass validation.
            raise ReconciliationError("Terminal result requires evidence", effect_id=effect_id)

        cursor = conn.execute(
            """
            UPDATE effects
            SET status = ?, mode = ?, resolved_at = ?, reconciliation_evidence = ?
            WHERE effect_id = ? AND status IN (?, ?)
            """,
            (
                EffectStatus.SOMETHING_LANDED.value,
                ExecutionMode.ACTIVE.value,
                resolved_at,
                evidence.to_json(),
                effect_id,
                EffectStatus.DISPATCHED_UNCONFIRMED.value,
                EffectStatus.INDETERMINATE.value,
            ),
        )
        if cursor.rowcount != 1:
            raise InvalidStateTransitionError(
                "Reconciliation result became stale", effect_id=effect_id
            )

        # Consume authority
        self._update_authority_for_resolution(
            conn,
            effect_id,
            AuthorityDisposition.CONSUMED,
            datetime.now(timezone.utc).isoformat(),
        )

        self._append_event(
            conn,
            effect_id,
            "reconciled_something_landed",
            evidence_reference=evidence,
            metadata={"explanation": result.explanation},
        )

    def _apply_still_indeterminate(
        self, conn: sqlite3.Connection, effect_id: str, result: ReconciliationResult
    ) -> None:
        """Apply still-indeterminate reconciliation result."""
        # A direct reconciliation of dispatched_unconfirmed may itself discover
        # uncertainty. Enter fail-safe mode and hold authority in that case.
        cursor = conn.execute(
            """
            UPDATE effects
            SET status = ?, mode = ?
            WHERE effect_id = ? AND status IN (?, ?)
            """,
            (
                EffectStatus.INDETERMINATE.value,
                ExecutionMode.FAIL_SAFE_PLAN_MODE.value,
                effect_id,
                EffectStatus.DISPATCHED_UNCONFIRMED.value,
                EffectStatus.INDETERMINATE.value,
            ),
        )
        if cursor.rowcount != 1:
            raise InvalidStateTransitionError(
                "Reconciliation result became stale", effect_id=effect_id
            )

        changed_at = datetime.now(timezone.utc).isoformat()
        authority_cursor = conn.execute(
            """
            UPDATE authority_reservations
            SET disposition = ?, disposition_changed_at = ?
            WHERE effect_id = ? AND disposition IN (?, ?)
            """,
            (
                AuthorityDisposition.HELD_UNRECONCILED.value,
                changed_at,
                effect_id,
                AuthorityDisposition.RESERVED.value,
                AuthorityDisposition.HELD_UNRECONCILED.value,
            ),
        )
        if authority_cursor.rowcount != 1:
            raise ReconciliationError(
                "Authority is not pending reconciliation", effect_id=effect_id
            )

        self._append_event(
            conn,
            effect_id,
            "reconciliation_still_indeterminate",
            evidence_reference=result.evidence_reference,
            metadata={"explanation": result.explanation},
        )

    def _get_intent(self, effect_id: str) -> EffectIntent:
        """Get the intent for an effect."""
        with self._transaction() as conn:
            row = conn.execute(
                """
                SELECT effect_id, mission_id, authority_id, operation, target,
                       payload_digest, idempotency_key, created_at
                FROM effects
                WHERE effect_id = ?
                """,
                (effect_id,),
            ).fetchone()

            if not row:
                raise EffectNotFoundError(effect_id)

            return EffectIntent(
                effect_id=row["effect_id"],
                mission_id=row["mission_id"],
                authority_id=row["authority_id"],
                operation=row["operation"],
                target=row["target"],
                payload_digest=row["payload_digest"],
                idempotency_key=row["idempotency_key"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )

    def get_effect(self, effect_id: str) -> Effect:
        """Get the current state of an effect.

        Args:
            effect_id: ID of the effect

        Returns:
            Effect with current state

        Raises:
            EffectNotFoundError: If effect doesn't exist
        """
        intent = self._get_intent(effect_id)

        with self._transaction() as conn:
            row = conn.execute(
                """
                SELECT status, mode, dispatched_at, resolved_at, reconciliation_evidence
                FROM effects
                WHERE effect_id = ?
                """,
                (effect_id,),
            ).fetchone()

            if not row:
                raise EffectNotFoundError(effect_id)

            return Effect(
                intent=intent,
                status=EffectStatus(row["status"]),
                mode=ExecutionMode(row["mode"]),
                dispatched_at=(
                    datetime.fromisoformat(row["dispatched_at"])
                    if row["dispatched_at"]
                    else None
                ),
                resolved_at=(
                    datetime.fromisoformat(row["resolved_at"])
                    if row["resolved_at"]
                    else None
                ),
                reconciliation_evidence=(
                    EvidenceReference.from_json(row["reconciliation_evidence"])
                    if row["reconciliation_evidence"]
                    else None
                ),
            )

    def get_authority(self, effect_id: str) -> AuthorityReservation:
        """Get the authority reservation for an effect.

        Args:
            effect_id: ID of the effect

        Returns:
            AuthorityReservation

        Raises:
            EffectNotFoundError: If effect doesn't exist
        """
        with self._transaction() as conn:
            row = conn.execute(
                """
                SELECT authority_id, effect_id, disposition, reserved_at, disposition_changed_at
                FROM authority_reservations
                WHERE effect_id = ?
                """,
                (effect_id,),
            ).fetchone()

            if not row:
                raise EffectNotFoundError(effect_id)

            return AuthorityReservation(
                authority_id=row["authority_id"],
                effect_id=row["effect_id"],
                disposition=AuthorityDisposition(row["disposition"]),
                reserved_at=datetime.fromisoformat(row["reserved_at"]),
                disposition_changed_at=datetime.fromisoformat(row["disposition_changed_at"])
                if row["disposition_changed_at"]
                else None,
            )

    def events_for(self, effect_id: str) -> list[LedgerEvent]:
        """Get the chronological event timeline for an effect.

        Args:
            effect_id: ID of the effect

        Returns:
            List of LedgerEvents in chronological order

        Raises:
            EffectNotFoundError: If effect doesn't exist
        """
        # Verify effect exists
        self._get_intent(effect_id)

        with self._transaction() as conn:
            rows = conn.execute(
                """
                SELECT sequence, effect_id, event_type, timestamp, evidence_reference, metadata_json
                FROM ledger_events
                WHERE effect_id = ?
                ORDER BY sequence ASC
                """,
                (effect_id,),
            ).fetchall()

            events = []
            for row in rows:
                metadata = json.loads(row["metadata_json"]) if row["metadata_json"] else {}
                events.append(
                    LedgerEvent(
                        sequence=row["sequence"],
                        effect_id=row["effect_id"],
                        event_type=row["event_type"],
                        timestamp=datetime.fromisoformat(row["timestamp"]),
                        evidence_reference=(
                            EvidenceReference.from_json(row["evidence_reference"])
                            if row["evidence_reference"]
                            else None
                        ),
                        metadata=metadata,
                    )
                )

            return events
