from collections.abc import Callable

import pytest
from pydantic import ValidationError

from eva_ai.actions.canonical import CanonicalEmail, canonical_email_hash


def test_canonical_email_normalizes_addresses_and_content() -> None:
    message = CanonicalEmail(
        mode="NEW",
        to=("  Alice <ALICE@Example.COM> ", "alice@example.com", "bob@EXAMPLE.com"),
        cc=(" Carol@Example.com ",),
        subject="  Project   update  ",
        text_body="Hello, Alice.  \r\n\r\nRegards,  \r\n",
        html_body="  <p>Hello, Alice.</p>  ",
    )

    assert message.to == ("alice@example.com", "bob@example.com")
    assert message.cc == ("carol@example.com",)
    assert message.subject == "Project update"
    assert message.text_body == "Hello, Alice.  \n\nRegards,"
    assert message.html_body == "<p>Hello, Alice.</p>"
    assert message.attachments == ()


@pytest.mark.parametrize(
    "change",
    [
        lambda value: value.model_copy(update={"to": ("other@example.com",)}),
        lambda value: value.model_copy(update={"cc": ("copy@example.com",)}),
        lambda value: value.model_copy(update={"bcc": ("blind@example.com",)}),
        lambda value: value.model_copy(update={"subject": "Different"}),
        lambda value: value.model_copy(update={"text_body": "Different"}),
        lambda value: value.model_copy(update={"html_body": "<p>Different</p>"}),
        lambda value: value.model_copy(update={"mode": "REPLY"}),
        lambda value: value.model_copy(update={"thread_id": "thread-2"}),
        lambda value: value.model_copy(update={"in_reply_to": "<message-2@example.com>"}),
        lambda value: value.model_copy(update={"references": ("<ref@example.com>",)}),
    ],
)
def test_canonical_hash_covers_every_send_affecting_field(
    change: Callable[[CanonicalEmail], CanonicalEmail],
) -> None:
    message = CanonicalEmail(
        mode="NEW",
        to=("person@example.com",),
        subject="Subject",
        text_body="Body",
    )

    assert canonical_email_hash(change(message)) != canonical_email_hash(message)


def test_canonical_hash_is_stable_for_equivalent_input() -> None:
    first = CanonicalEmail(
        mode="NEW",
        to=("Person <PERSON@example.com>",),
        subject="  A  subject ",
        text_body="Body\r\n",
    )
    second = CanonicalEmail(
        mode="NEW",
        to=("person@example.com",),
        subject="A subject",
        text_body="Body",
    )

    assert canonical_email_hash(first) == canonical_email_hash(second)


def test_canonical_email_rejects_attachments() -> None:
    with pytest.raises(ValidationError, match="attachments are not supported"):
        CanonicalEmail(
            mode="NEW",
            to=("person@example.com",),
            subject="Subject",
            text_body="Body",
            attachments=("file.pdf",),
        )


@pytest.mark.parametrize("address", ["", "not-an-email", "Name <>", "a@example.com, b@example.com"])
def test_canonical_email_rejects_invalid_recipient(address: str) -> None:
    with pytest.raises(ValidationError, match="valid email address"):
        CanonicalEmail(mode="NEW", to=(address,), subject="Subject", text_body="Body")


def test_canonical_email_requires_a_recipient() -> None:
    with pytest.raises(ValidationError, match="at least one recipient"):
        CanonicalEmail(mode="NEW", to=(), subject="Subject", text_body="Body")


def test_reply_requires_trusted_thread_context() -> None:
    with pytest.raises(ValidationError, match="reply context"):
        CanonicalEmail(
            mode="REPLY",
            to=("person@example.com",),
            subject="Re: Subject",
            text_body="Body",
        )


def test_new_email_rejects_reply_context() -> None:
    with pytest.raises(ValidationError, match="new email cannot include reply context"):
        CanonicalEmail(
            mode="NEW",
            to=("person@example.com",),
            subject="Subject",
            text_body="Body",
            thread_id="thread-1",
        )
