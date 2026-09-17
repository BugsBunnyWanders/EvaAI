from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import uuid7

import pytest

from eva_ai.actions.canonical import CanonicalEmail
from eva_ai.actions.errors import ActionValidationError
from eva_ai.actions.service import ActionRevisionService
from eva_ai.actions.types import AllowedActionCreation, DraftRevisionCandidate, RevisionLookup

NOW = datetime(2030, 1, 1, tzinfo=UTC)


def _reply() -> CanonicalEmail:
    return CanonicalEmail(
        mode="REPLY",
        to=("original@example.com",),
        subject="Re: Existing thread",
        text_body="Original body",
        thread_id="thread-1",
        in_reply_to="<message-1@example.com>",
        references=("<message-1@example.com>",),
    )


def test_complete_replacement_preserves_trusted_reply_context() -> None:
    candidate = DraftRevisionCandidate(
        mode="REPLY",
        to=("updated@example.com",),
        cc=("copy@example.com",),
        bcc=(),
        subject="Re: Updated subject",
        text_body="A completely revised body.",
        html_body=None,
        thread_id="thread-1",
        attachments=(),
    )

    replacement = ActionRevisionService.validate_replacement(_reply(), candidate)

    assert replacement.to == ("updated@example.com",)
    assert replacement.text_body == "A completely revised body."
    assert replacement.thread_id == "thread-1"
    assert replacement.in_reply_to == "<message-1@example.com>"
    assert replacement.references == ("<message-1@example.com>",)


@pytest.mark.parametrize(
    "candidate",
    [
        DraftRevisionCandidate(
            mode="REPLY",
            to=(),
            subject="Re: Existing thread",
            text_body="Body",
            thread_id="thread-1",
        ),
        DraftRevisionCandidate(
            mode="REPLY",
            to=("updated@example.com",),
            subject="Re: Existing thread",
            text_body="Body",
            thread_id="another-thread",
        ),
        DraftRevisionCandidate(
            mode="REPLY",
            to=("updated@example.com",),
            subject="Re: Existing thread",
            text_body="Body",
            thread_id="thread-1",
            attachments=("file.pdf",),
        ),
    ],
)
def test_invalid_or_cross_thread_replacement_is_rejected(
    candidate: DraftRevisionCandidate,
) -> None:
    with pytest.raises(ActionValidationError, match="revision"):
        ActionRevisionService.validate_replacement(_reply(), candidate)


class Store:
    def __init__(self) -> None:
        self.completed: tuple[object, ...] | None = None

    async def load_revision(self, **values: object) -> RevisionLookup:
        raise AssertionError(f"revision lookup was not expected: {tuple(values)}")

    async def complete_revision(self, **values: object) -> AllowedActionCreation:
        self.completed = tuple(values.values())
        return cast(AllowedActionCreation, object())


async def test_completion_passes_validated_message_to_atomic_store() -> None:
    store = Store()
    service = ActionRevisionService(store, destination="eva-events")
    session_id = uuid7()
    current = _reply()
    candidate = DraftRevisionCandidate(
        mode="REPLY",
        to=("updated@example.com",),
        subject="Re: Existing thread",
        text_body="Revised body",
        thread_id="thread-1",
    )

    await service.complete(
        session_id=session_id,
        current=current,
        candidate=candidate,
        now=NOW + timedelta(minutes=1),
    )

    assert store.completed is not None
    assert session_id in store.completed
    replacement = next(value for value in store.completed if isinstance(value, CanonicalEmail))
    assert replacement.text_body == "Revised body"
