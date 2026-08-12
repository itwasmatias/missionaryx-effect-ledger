"""Domain-specific exceptions for the effect ledger."""


class LedgerError(Exception):
    """Base exception for all ledger errors."""

    pass


class InvalidStateTransitionError(LedgerError):
    """Raised when an operation would cause an illegal state transition."""

    def __init__(self, message: str, effect_id: str | None = None):
        super().__init__(message)
        self.effect_id = effect_id


class AuthorityAlreadyUsedError(LedgerError):
    """Raised when attempting to use authority that is already reserved or consumed."""

    def __init__(self, authority_id: str, existing_effect_id: str):
        super().__init__(
            f"Authority {authority_id} is already used by effect {existing_effect_id}"
        )
        self.authority_id = authority_id
        self.existing_effect_id = existing_effect_id


class IdempotencyKeyConflictError(LedgerError):
    """Raised when attempting to create an effect with a duplicate idempotency key."""

    def __init__(self, idempotency_key: str, existing_effect_id: str):
        super().__init__(
            f"Idempotency key '{idempotency_key}' is already used by effect {existing_effect_id}"
        )
        self.idempotency_key = idempotency_key
        self.existing_effect_id = existing_effect_id


class DispatchNotAllowedError(LedgerError):
    """Raised when attempting to dispatch an effect that cannot be dispatched."""

    def __init__(self, effect_id: str, reason: str):
        super().__init__(f"Cannot dispatch effect {effect_id}: {reason}")
        self.effect_id = effect_id
        self.reason = reason


class ReconciliationError(LedgerError):
    """Raised when reconciliation fails or produces invalid results."""

    def __init__(self, message: str, effect_id: str | None = None):
        super().__init__(message)
        self.effect_id = effect_id


class EffectNotFoundError(LedgerError):
    """Raised when an effect is not found in the ledger."""

    def __init__(self, effect_id: str):
        super().__init__(f"Effect {effect_id} not found")
        self.effect_id = effect_id
