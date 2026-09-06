import re

from eva_ai.memory.errors import UnsafeMemoryError
from eva_ai.memory.types import MemoryFactDraft

_TOKEN = re.compile(r"[a-z0-9]+")
_FORBIDDEN_TERMS = {
    "api",
    "authorization",
    "credential",
    "credentials",
    "key",
    "oauth",
    "password",
    "private",
    "secret",
    "token",
}


class MemoryPolicy:
    def validate_fact(self, draft: MemoryFactDraft) -> None:
        # Slot names are policy-controlled metadata; rejecting sensitive slots is a deterministic
        # backstop against accidentally turning Secret Manager material into durable memory.
        terms = set(_TOKEN.findall(f"{draft.namespace}.{draft.key}"))
        if terms & _FORBIDDEN_TERMS:
            raise UnsafeMemoryError()
