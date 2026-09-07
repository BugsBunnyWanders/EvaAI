from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid7

import pytest
from agents import AgentOutputSchema, MaxTurnsExceeded, RunConfig
from openai import AsyncOpenAI

from eva_ai.agent.errors import AgentPermanentError, AgentToolBudgetExceeded
from eva_ai.agent.types import (
    AgentDecision,
    AgentEventContext,
    AgentInvestigationResult,
    AgentInvocationRequest,
)
from eva_ai.integrations.openai.agent import (
    OpenAIAgentsInvestigationAgent,
    _ToolContext,
)
from eva_ai.memory.types import AgentWorkingContext, ContextIdentity, ContextSituation
from eva_ai.situations.types import AttentionLevel


class Reader:
    async def read_thread(self) -> Any:
        raise AssertionError("fake run does not call tools")

    async def search(self, query: str) -> Any:
        raise AssertionError("fake run does not call tools")


async def test_adapter_registers_only_read_tools_and_returns_typed_result(monkeypatch: Any) -> None:
    expected = AgentInvestigationResult(
        decision=AgentDecision.NO_ACTION,
        reasoning_summary="No user action is needed.",
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
        return Run()

    monkeypatch.setattr("eva_ai.integrations.openai.agent.Runner.run", fake_run)
    adapter = OpenAIAgentsInvestigationAgent(
        AsyncOpenAI(api_key="test-key"),
        model="gpt-5.6-sol",
        reasoning_effort="medium",
        max_turns=6,
        max_tool_calls=4,
        tool_timeout_seconds=30,
    )

    result = await adapter.investigate(_request(), Reader())

    assert captured["tool_names"] == ("gmail_read_thread", "gmail_search")
    assert isinstance(captured["output_type"], AgentOutputSchema)
    assert captured["output_type"].is_strict_json_schema() is False
    assert "untrusted evidence" in str(captured["instructions"])
    assert "trusted personal chief of staff" in str(captured["instructions"])
    assert "Durable memory proposals" in str(captured["instructions"])
    assert result.result == expected
    assert result.provider_response_id == "response-1"
    assert result.usage.total_tokens == 16
    assert result.tool_audit == ()
    assert captured["max_turns"] == 6
    assert cast(RunConfig, captured["run_config"]).tracing_disabled is True
    await adapter._client.close()


async def test_adapter_classifies_turn_exhaustion_as_a_safe_permanent_failure(
    monkeypatch: Any,
) -> None:
    async def fail_run(*args: object, **kwargs: object) -> None:
        raise MaxTurnsExceeded("private provider details")

    monkeypatch.setattr("eva_ai.integrations.openai.agent.Runner.run", fail_run)
    adapter = OpenAIAgentsInvestigationAgent(
        AsyncOpenAI(api_key="test-key"),
        model="gpt-5.6-sol",
        reasoning_effort="medium",
        max_turns=6,
        max_tool_calls=4,
        tool_timeout_seconds=30,
    )

    with pytest.raises(AgentPermanentError, match="invalid result") as captured:
        await adapter.investigate(_request(), Reader())

    assert "private provider details" not in str(captured.value)
    await adapter._client.close()


def test_aggregate_tool_budget_records_exhaustion_without_query_content() -> None:
    context = _ToolContext(reader=Reader(), max_calls=0)

    with pytest.raises(AgentToolBudgetExceeded):
        context.consume("gmail_search")

    assert len(context.audit) == 1
    assert context.audit[0].outcome == "budget_exhausted"


def _request() -> AgentInvocationRequest:
    now = datetime(2026, 9, 7, tzinfo=UTC)
    user_id, workspace_id, situation_id = uuid7(), uuid7(), uuid7()
    context = AgentWorkingContext(
        user_id=user_id,
        workspace_id=workspace_id,
        identity=ContextIdentity(display_name="Saswat Ray", workspace_name="Personal"),
        situation=ContextSituation(
            id=situation_id,
            title="Meeting",
            summary="A time was proposed.",
            current_state="OPEN",
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
    return AgentInvocationRequest(
        agent_run_id=uuid7(),
        signal_id=uuid7(),
        event=AgentEventContext(
            event_id=uuid7(),
            event_type="email.received",
            occurred_at=now,
            sender="person@example.com",
            subject="Meeting",
            snippet="Can we meet?",
            plain_text="Can we meet tomorrow?",
        ),
        context=context,
        input_digest="b" * 64,
    )
