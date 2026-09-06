from datetime import UTC, datetime

import pytest

from eva_ai.memory.embedding import EmbeddingService, ScriptedEmbeddingProvider
from eva_ai.memory.errors import MemoryEmbeddingError

NOW = datetime(2026, 9, 6, tzinfo=UTC)


async def test_embedding_service_returns_validated_vector_and_digest() -> None:
    provider = ScriptedEmbeddingProvider(((0.1, 0.2, 0.3),))
    service = EmbeddingService(
        provider=provider,
        model="embedding-test",
        dimensions=3,
        input_max_chars=20,
        clock=lambda: NOW,
    )

    result = await service.embed("  remembered decision  ")

    assert result.values == (0.1, 0.2, 0.3)
    assert result.model == "embedding-test"
    assert result.dimensions == 3
    assert len(result.input_digest) == 64
    assert result.embedded_at == NOW
    assert provider.inputs == ["remembered decision"]


@pytest.mark.parametrize("vector", [(0.1,), (0.1, float("nan"), 0.3)])
async def test_embedding_service_rejects_invalid_vectors(vector: tuple[float, ...]) -> None:
    service = EmbeddingService(
        ScriptedEmbeddingProvider((vector,)), "embedding-test", 3, 20, lambda: NOW
    )

    with pytest.raises(MemoryEmbeddingError):
        await service.embed("content")


async def test_embedding_service_hides_provider_failures() -> None:
    marker = "provider-secret-response"
    service = EmbeddingService(
        ScriptedEmbeddingProvider((RuntimeError(marker),)),
        "embedding-test",
        3,
        20,
        lambda: NOW,
    )

    with pytest.raises(MemoryEmbeddingError) as raised:
        await service.embed("content")

    assert marker not in str(raised.value)


async def test_embedding_service_enforces_nonblank_bounded_input() -> None:
    service = EmbeddingService(
        ScriptedEmbeddingProvider(((0.1, 0.2, 0.3),)),
        "embedding-test",
        3,
        5,
        lambda: NOW,
    )

    with pytest.raises(MemoryEmbeddingError):
        await service.embed(" ")
    with pytest.raises(MemoryEmbeddingError):
        await service.embed("123456")
