from pydantic import BaseModel, ConfigDict


class BackboneError(RuntimeError):
    pass


class UnknownEventError(BackboneError):
    pass


class ScopeMismatchError(BackboneError):
    pass


class StaleClaimError(BackboneError):
    pass


class StoredError(BaseModel):
    model_config = ConfigDict(frozen=True)

    error_type: str
    summary: str


def sanitize_error(error: BaseException) -> StoredError:
    # Exception messages can contain DSNs or provider payloads, so persistence keeps no text.
    return StoredError(error_type=type(error).__name__, summary="operation failed")
