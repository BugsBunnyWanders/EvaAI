from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import JsonValue

from eva_ai.events.processor import StoredEvent
from eva_ai.relevance.filters import (
    RelevanceRuleSet,
    ScreeningFacts,
    StaticRelevanceRuleProvider,
    screen_event,
)
from eva_ai.relevance.types import ScreeningReason

USER_ID = UUID("00000000-0000-7000-8000-000000000001")
WORKSPACE_ID = UUID("00000000-0000-7000-8000-000000000002")
EVENT_ID = UUID("00000000-0000-7000-8000-000000000003")


def gmail_event(
    *,
    labels: tuple[str, ...] = ("INBOX",),
    sender: str = "Sender <sender@example.com>",
    source: str = "gmail",
    event_type: str = "email.received",
    schema_version: int = 1,
    message_id: JsonValue = "message-1",
    thread_id: JsonValue = "thread-1",
) -> StoredEvent:
    return StoredEvent(
        id=EVENT_ID,
        user_id=USER_ID,
        workspace_id=WORKSPACE_ID,
        source=source,
        event_type=event_type,
        external_id=str(message_id) if isinstance(message_id, str) else None,
        occurred_at=datetime(2026, 9, 1, tzinfo=UTC),
        payload={
            "message_id": message_id,
            "thread_id": thread_id,
            "headers": {"from": sender, "subject": ""},
            "label_ids": list(labels),
            "plain_text": "",
        },
        correlation_keys=("gmail-thread:thread-1",),
        schema_version=schema_version,
    )


def test_promotions_label_is_not_a_builtin_ignore() -> None:
    decision = screen_event(
        gmail_event(labels=("INBOX", "CATEGORY_PROMOTIONS")),
        ScreeningFacts(),
        RelevanceRuleSet(),
    )
    assert decision is None


@pytest.mark.parametrize(
    ("rules", "reason"),
    [
        (RelevanceRuleSet(ignored_sources=("GMAIL",)), ScreeningReason.IGNORED_SOURCE),
        (
            RelevanceRuleSet(ignored_event_types=("EMAIL.RECEIVED",)),
            ScreeningReason.IGNORED_EVENT_TYPE,
        ),
        (
            RelevanceRuleSet(ignored_senders=("SENDER@EXAMPLE.COM",)),
            ScreeningReason.IGNORED_SENDER,
        ),
        (RelevanceRuleSet(ignored_labels=("inbox",)), ScreeningReason.IGNORED_LABEL),
    ],
)
def test_exact_explicit_rules_ignore_without_ai(
    rules: RelevanceRuleSet, reason: ScreeningReason
) -> None:
    decision = screen_event(gmail_event(), ScreeningFacts(), rules)
    assert decision is not None and decision.reason is reason


@pytest.mark.parametrize(
    "event",
    [
        gmail_event(source="calendar"),
        gmail_event(event_type="email.sent"),
        gmail_event(schema_version=2),
    ],
)
def test_unsupported_events_are_screened(event: StoredEvent) -> None:
    decision = screen_event(event, ScreeningFacts(), RelevanceRuleSet())
    assert decision is not None and decision.reason is ScreeningReason.UNSUPPORTED_EVENT


@pytest.mark.parametrize(
    "event",
    [gmail_event(message_id=""), gmail_event(thread_id=""), gmail_event(message_id=12)],
)
def test_missing_gmail_identity_is_malformed(event: StoredEvent) -> None:
    decision = screen_event(event, ScreeningFacts(), RelevanceRuleSet())
    assert decision is not None and decision.reason is ScreeningReason.MALFORMED_EVENT


def test_duplicate_fact_has_first_precedence() -> None:
    duplicate_id = UUID("00000000-0000-7000-8000-000000000009")
    decision = screen_event(
        gmail_event(source="unsupported"),
        ScreeningFacts(duplicate_of_event_id=duplicate_id),
        RelevanceRuleSet(),
    )
    assert decision is not None and decision.reason is ScreeningReason.DUPLICATE_EVENT


async def test_static_rule_provider_keeps_normalized_immutable_rules() -> None:
    provider = StaticRelevanceRuleProvider(RelevanceRuleSet(ignored_labels=(" Inbox ", "INBOX")))
    rules = await provider.for_scope(user_id=USER_ID, workspace_id=WORKSPACE_ID)
    assert rules.ignored_labels == ("inbox",)
