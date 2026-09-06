import hashlib
import json
import re
from dataclasses import dataclass
from email.utils import parseaddr
from typing import Protocol
from uuid import UUID

from pydantic import JsonValue

from eva_ai.events.processor import StoredEvent
from eva_ai.relevance.types import (
    EvaluationContext,
    EventContext,
    GoalContext,
    SituationContext,
)

_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class ContextBounds:
    body_max_chars: int = 4000
    goal_limit: int = 20
    goal_max_chars: int = 500
    goal_total_chars: int = 8000
    situation_limit: int = 5
    situation_max_chars: int = 500
    situation_total_chars: int = 2500


class ContextRepository(Protocol):
    async def list_active_goal_contexts(
        self, *, user_id: UUID, workspace_id: UUID, limit: int
    ) -> tuple[GoalContext, ...]: ...

    async def list_situation_contexts(
        self,
        *,
        user_id: UUID,
        workspace_id: UUID,
        correlation_keys: tuple[str, ...],
        goal_ids: tuple[UUID, ...],
        limit: int,
    ) -> tuple[SituationContext, ...]: ...


class RelevanceContextBuilder:
    def __init__(self, repository: ContextRepository, bounds: ContextBounds) -> None:
        self._repository = repository
        self._bounds = bounds

    async def build(self, event: StoredEvent) -> EvaluationContext:
        goals = await self._repository.list_active_goal_contexts(
            user_id=event.user_id,
            workspace_id=event.workspace_id,
            limit=self._bounds.goal_limit,
        )
        bounded_goals = _bound_goals(
            goals, self._bounds.goal_max_chars, self._bounds.goal_total_chars
        )
        situations = await self._repository.list_situation_contexts(
            user_id=event.user_id,
            workspace_id=event.workspace_id,
            correlation_keys=event.correlation_keys,
            goal_ids=tuple(goal.id for goal in bounded_goals),
            limit=self._bounds.situation_limit,
        )
        return EvaluationContext(
            event_id=event.id,
            user_id=event.user_id,
            workspace_id=event.workspace_id,
            event=_event_context(event, self._bounds.body_max_chars),
            goals=bounded_goals,
            situations=_bound_situations(
                situations,
                self._bounds.situation_max_chars,
                self._bounds.situation_total_chars,
            ),
        )


def serialize_context(context: EvaluationContext) -> str:
    return json.dumps(
        context.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def context_digest(context: EvaluationContext) -> str:
    return hashlib.sha256(serialize_context(context).encode("utf-8")).hexdigest()


def _event_context(event: StoredEvent, body_max_chars: int) -> EventContext:
    payload = event.payload
    headers = _mapping(payload.get("headers"))
    sender_name, sender_address = parseaddr(_text(headers.get("from")))
    labels_value = payload.get("label_ids")
    labels = (
        tuple(_header(item) for item in labels_value if isinstance(item, str))
        if isinstance(labels_value, list)
        else ()
    )
    return EventContext(
        source=_header(event.source),
        event_type=_header(event.event_type),
        occurred_at=event.occurred_at,
        sender_name=_header(sender_name)[:500],
        sender_address=_header(sender_address)[:500],
        subject=_header(_text(headers.get("subject")))[:1000],
        snippet=_header(_text(payload.get("snippet")))[:2000],
        label_ids=labels,
        plain_text=_plain_text(_text(payload.get("plain_text")))[:body_max_chars],
    )


def _bound_goals(
    values: tuple[GoalContext, ...], per_item: int, total: int
) -> tuple[GoalContext, ...]:
    result: list[GoalContext] = []
    remaining = total
    for value in values:
        if remaining <= 0:
            break
        title = _header(value.title)[: min(per_item, remaining)]
        remaining -= len(title)
        summary = _header(value.summary)[: min(per_item, remaining)]
        remaining -= len(summary)
        domain = _header(value.domain)[: min(100, remaining)]
        remaining -= len(domain)
        if title and summary and domain:
            result.append(
                value.model_copy(update={"title": title, "summary": summary, "domain": domain})
            )
    return tuple(result)


def _bound_situations(
    values: tuple[SituationContext, ...], per_item: int, total: int
) -> tuple[SituationContext, ...]:
    result: list[SituationContext] = []
    remaining = total
    for value in values:
        if remaining <= 0:
            break
        title = _header(value.title)[: min(per_item, remaining)]
        remaining -= len(title)
        summary = _header(value.summary)[: min(per_item, remaining)]
        remaining -= len(summary)
        state = _header(value.current_state)[: min(100, remaining)]
        remaining -= len(state)
        if title and state:
            result.append(
                value.model_copy(
                    update={"title": title, "summary": summary, "current_state": state}
                )
            )
    return tuple(result)


def _mapping(value: JsonValue | None) -> dict[str, JsonValue]:
    return value if isinstance(value, dict) else {}


def _text(value: JsonValue | None) -> str:
    return value if isinstance(value, str) else ""


def _header(value: str) -> str:
    return _WHITESPACE.sub(" ", value).strip()


def _plain_text(value: str) -> str:
    lines = (_header(line) for line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n"))
    return "\n".join(line for line in lines if line)
