from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid7

import pytest
from agents import AgentOutputSchema, MaxTurnsExceeded, RunConfig
from openai import AsyncOpenAI

from eva_ai.conversation.errors import ConversationPermanentError
from eva_ai.conversation.types import (
    ConversationAgentRequest,
    ConversationAgentResult,
    ConversationHistoryTurn,
    ConversationTurnRole,
)
from eva_ai.integrations.openai.conversation import OpenAIAgentsConversationAgent
from eva_ai.memory.types import AgentWorkingContext, ContextIdentity, ContextSituation
from eva_ai.situations.types import AttentionLevel


class Reader:
    async def read_thread(self) -> Any:
        raise AssertionError("fake run does not call tools")

    async def search(self, query: str) -> Any:
        raise AssertionError("fake run does not call tools")


@pytest.mark.parametrize(
    ("is_email_situation", "expected_tools"),
    [
        (False, ("gmail_search",)),
        (True, ("gmail_read_thread", "gmail_search")),
    ],
)
async def test_conversation_adapter_registers_only_context_appropriate_read_tools(
    monkeypatch: Any,
    is_email_situation: bool,
    expected_tools: tuple[str, ...],
) -> None:
    expected = ConversationAgentResult(
        message="Here is the scoped answer.",
        reasoning_summary="Used only the authenticated context.",
    )
    captured: dict[str, object] = {}

    class Run:
        final_output = expected
        last_response_id = "response-1"
        context_wrapper = SimpleNamespace(
            usage=SimpleNamespace(input_tokens=12, output_tokens=4, total_tokens=16)
        )

        def final_output_as(
            self, output_type: type[object], raise_if_incorrect_type: bool
        ) -> object:
            return self.final_output

    async def fake_run(agent: Any, input: str, **kwargs: object) -> Run:
        captured["tool_names"] = tuple(tool.name for tool in agent.tools)
        captured["output_type"] = agent.output_type
        captured["instructions"] = agent.instructions
        captured["run_config"] = kwargs["run_config"]
        captured["max_turns"] = kwargs["max_turns"]
        captured["input"] = input
        return Run()

    monkeypatch.setattr("eva_ai.integrations.openai.conversation.Runner.run", fake_run)
    adapter = OpenAIAgentsConversationAgent(
        AsyncOpenAI(api_key="test-key"),
        model="gpt-5.6-sol",
        reasoning_effort="medium",
        max_turns=6,
        max_tool_calls=4,
        tool_timeout_seconds=30,
    )

    result = await adapter.respond(_request(is_email_situation), Reader())

    assert captured["tool_names"] == expected_tools
    assert isinstance(captured["output_type"], AgentOutputSchema)
    assert captured["output_type"].is_strict_json_schema() is False
    assert "authenticated Eva user" in str(captured["instructions"])
    assert "untrusted evidence" in str(captured["instructions"])
    assert "trusted personal chief of staff" in str(captured["instructions"])
    assert "Durable memory proposals" in str(captured["instructions"])
    assert "Telegram output is plain text" in str(captured["instructions"])
    assert "Earlier answer" in str(captured["input"])
    assert result.result == expected
    assert result.usage.total_tokens == 16
    assert captured["max_turns"] == 6
    assert cast(RunConfig, captured["run_config"]).tracing_disabled is True
    await adapter._client.close()


async def test_conversation_adapter_safely_classifies_turn_exhaustion(monkeypatch: Any) -> None:
    async def fail_run(*args: object, **kwargs: object) -> None:
        raise MaxTurnsExceeded("private provider details")

    monkeypatch.setattr("eva_ai.integrations.openai.conversation.Runner.run", fail_run)
    adapter = OpenAIAgentsConversationAgent(
        AsyncOpenAI(api_key="test-key"),
        model="gpt-5.6-sol",
        reasoning_effort="medium",
        max_turns=6,
        max_tool_calls=4,
        tool_timeout_seconds=30,
    )

    with pytest.raises(ConversationPermanentError, match="invalid output") as captured:
        await adapter.respond(_request(False), None)

    assert "private provider details" not in str(captured.value)
    await adapter._client.close()


def _request(is_email_situation: bool) -> ConversationAgentRequest:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    user_id, workspace_id, situation_id = uuid7(), uuid7(), uuid7()
    context = AgentWorkingContext(
        user_id=user_id,
        workspace_id=workspace_id,
        identity=ContextIdentity(display_name="Saswat Ray", workspace_name="Personal"),
        situation=ContextSituation(
            id=situation_id,
            title="Conversation",
            summary="A scoped chat.",
            current_state="CONVERSING",
            next_action=None,
            next_expected=None,
            attention=AttentionLevel.NORMAL,
            last_activity_at=now,
        ),
        goals=(),
        facts=(),
        episodes=(),
        built_at=now,
        digest="a" * 64,
    )
    return ConversationAgentRequest(
        turn_id=uuid7(),
        conversation_id=uuid7(),
        message="What should I do?",
        history=(
            ConversationHistoryTurn(
                role=ConversationTurnRole.ASSISTANT,
                text="Earlier answer",
            ),
        ),
        context=context,
        is_email_situation=is_email_situation,
    )
