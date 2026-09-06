import hashlib
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from eva_ai.memory.errors import MemoryEmbeddingError


class EmbeddingProvider(Protocol):
    async def embed(self, text: str) -> tuple[float, ...]: ...


@dataclass(frozen=True, slots=True)
class EmbeddedText:
    values: tuple[float, ...]
    model: str
    dimensions: int
    input_digest: str
    embedded_at: datetime


class EmbeddingService:
    def __init__(
        self,
        provider: EmbeddingProvider,
        model: str,
        dimensions: int,
        input_max_chars: int,
        clock: Callable[[], datetime],
    ) -> None:
        self._provider = provider
        self._model = model
        self._dimensions = dimensions
        self._input_max_chars = input_max_chars
        self._clock = clock

    async def embed(self, text: str) -> EmbeddedText:
        normalized = text.strip()
        if not normalized or len(normalized) > self._input_max_chars:
            raise MemoryEmbeddingError()
        try:
            values = await self._provider.embed(normalized)
        except Exception:
            raise MemoryEmbeddingError() from None
        if len(values) != self._dimensions or any(not math.isfinite(value) for value in values):
            raise MemoryEmbeddingError()
        return EmbeddedText(
            values=values,
            model=self._model,
            dimensions=self._dimensions,
            input_digest=hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
            embedded_at=self._clock(),
        )


class ScriptedEmbeddingProvider:
    def __init__(self, script: tuple[tuple[float, ...] | Exception, ...]) -> None:
        self._script = list(script)
        self.inputs: list[str] = []

    async def embed(self, text: str) -> tuple[float, ...]:
        self.inputs.append(text)
        if not self._script:
            raise AssertionError("embedding script exhausted")
        result = self._script.pop(0)
        if isinstance(result, Exception):
            raise result
        return result
