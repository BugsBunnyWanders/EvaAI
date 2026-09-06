from typing import Any, Protocol


class EmbeddingsAPI(Protocol):
    async def create(self, **kwargs: Any) -> Any: ...


class OpenAIEmbeddingClient(Protocol):
    @property
    def embeddings(self) -> EmbeddingsAPI: ...


class OpenAIEmbeddingProvider:
    def __init__(self, client: OpenAIEmbeddingClient, model: str, dimensions: int) -> None:
        self._client = client
        self._model = model
        self._dimensions = dimensions

    async def embed(self, text: str) -> tuple[float, ...]:
        response = await self._client.embeddings.create(
            model=self._model,
            input=text,
            dimensions=self._dimensions,
            encoding_format="float",
        )
        return tuple(float(value) for value in response.data[0].embedding)
