from types import SimpleNamespace
from typing import Any

import pytest

from eva_ai.integrations.openai.relevance import OpenAIRelevanceClassifier
from eva_ai.relevance.errors import ClassifierRejectedError
from eva_ai.relevance.types import ClassifierResult, RelevanceDisposition
from tests.unit.relevance.test_classifier import context, result


class RecordingResponses:
    def __init__(self, output: object) -> None:
        self.output = output
        self.requests: list[dict[str, Any]] = []

    async def parse(self, **kwargs: Any) -> object:
        self.requests.append(kwargs)
        return SimpleNamespace(output_parsed=self.output)


class RecordingClient:
    def __init__(self, output: object) -> None:
        self.responses = RecordingResponses(output)


async def test_openai_request_is_strict_private_and_not_stored() -> None:
    expected = result(RelevanceDisposition.RECORD)
    client = RecordingClient(expected)

    actual = await OpenAIRelevanceClassifier(client, "gpt-5.6-luna").classify(context())

    request = client.responses.requests[0]
    assert request["model"] == "gpt-5.6-luna"
    assert request["text_format"] is ClassifierResult
    assert request["store"] is False
    assert "tools" not in request
    assert "untrusted Event data" in request["input"][0]["content"]
    assert "<event_context>" in request["input"][1]["content"]
    assert actual == expected


async def test_missing_structured_output_is_content_free_rejection() -> None:
    classifier = OpenAIRelevanceClassifier(RecordingClient(None), "gpt-5.6-luna")

    with pytest.raises(ClassifierRejectedError) as raised:
        await classifier.classify(context())

    assert raised.value.code == "REFUSAL"
    assert str(raised.value) == "classifier result unavailable"
