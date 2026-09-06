from dataclasses import dataclass
from email.utils import parseaddr
from typing import Protocol
from uuid import UUID

from eva_ai.events.processor import StoredEvent
from eva_ai.relevance.types import ScreeningReason


def _normalized(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted({value.strip().casefold() for value in values if value.strip()}))


@dataclass(frozen=True, slots=True)
class RelevanceRuleSet:
    ignored_sources: tuple[str, ...] = ()
    ignored_event_types: tuple[str, ...] = ()
    ignored_senders: tuple[str, ...] = ()
    ignored_labels: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "ignored_sources", _normalized(self.ignored_sources))
        object.__setattr__(self, "ignored_event_types", _normalized(self.ignored_event_types))
        object.__setattr__(self, "ignored_senders", _normalized(self.ignored_senders))
        object.__setattr__(self, "ignored_labels", _normalized(self.ignored_labels))


class RelevanceRuleProvider(Protocol):
    async def for_scope(self, *, user_id: UUID, workspace_id: UUID) -> RelevanceRuleSet:
        raise NotImplementedError


class StaticRelevanceRuleProvider:
    def __init__(self, rules: RelevanceRuleSet) -> None:
        self._rules = rules

    async def for_scope(self, *, user_id: UUID, workspace_id: UUID) -> RelevanceRuleSet:
        return self._rules


@dataclass(frozen=True, slots=True)
class ScreeningFacts:
    duplicate_of_event_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class ScreeningDecision:
    reason: ScreeningReason


def screen_event(
    event: StoredEvent,
    facts: ScreeningFacts,
    rules: RelevanceRuleSet,
) -> ScreeningDecision | None:
    if facts.duplicate_of_event_id is not None:
        return ScreeningDecision(ScreeningReason.DUPLICATE_EVENT)
    if event.source != "gmail" or event.event_type != "email.received" or event.schema_version != 1:
        return ScreeningDecision(ScreeningReason.UNSUPPORTED_EVENT)
    if not _has_required_gmail_identity(event):
        return ScreeningDecision(ScreeningReason.MALFORMED_EVENT)
    return _explicit_rule_decision(event, rules)


def _has_required_gmail_identity(event: StoredEvent) -> bool:
    return _nonblank_string(event.payload.get("message_id")) and _nonblank_string(
        event.payload.get("thread_id")
    )


def _nonblank_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _explicit_rule_decision(
    event: StoredEvent, rules: RelevanceRuleSet
) -> ScreeningDecision | None:
    if event.source.casefold() in rules.ignored_sources:
        return ScreeningDecision(ScreeningReason.IGNORED_SOURCE)
    if event.event_type.casefold() in rules.ignored_event_types:
        return ScreeningDecision(ScreeningReason.IGNORED_EVENT_TYPE)

    headers = event.payload.get("headers")
    sender = ""
    if isinstance(headers, dict):
        raw_sender = headers.get("from")
        if isinstance(raw_sender, str):
            sender = parseaddr(raw_sender)[1].strip().casefold()
    if sender and sender in rules.ignored_senders:
        return ScreeningDecision(ScreeningReason.IGNORED_SENDER)

    raw_labels = event.payload.get("label_ids")
    labels = (
        {
            value.strip().casefold()
            for value in raw_labels
            if isinstance(value, str) and value.strip()
        }
        if isinstance(raw_labels, list)
        else set()
    )
    if labels.intersection(rules.ignored_labels):
        return ScreeningDecision(ScreeningReason.IGNORED_LABEL)
    return None
