from types import SimpleNamespace
from typing import Any

from eva_ai.integrations.openai.memory import OpenAIEmbeddingProvider


class FakeEmbeddingsAPI:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}

    async def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        return SimpleNamespace(data=[SimpleNamespace(embedding=[0.1, 0.2, 0.3])])


class FakeClient:
    def __init__(self) -> None:
        self.embeddings = FakeEmbeddingsAPI()


async def test_openai_embedding_provider_uses_explicit_model_and_dimensions() -> None:
    client = FakeClient()
    provider = OpenAIEmbeddingProvider(client, "embedding-test", 3)

    result = await provider.embed("bounded summary")

    assert result == (0.1, 0.2, 0.3)
    assert client.embeddings.kwargs == {
        "model": "embedding-test",
        "input": "bounded summary",
        "dimensions": 3,
        "encoding_format": "float",
    }
