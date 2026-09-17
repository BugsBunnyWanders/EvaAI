import base64
from email import policy
from email.parser import BytesParser

from eva_ai.actions.canonical import CanonicalEmail
from eva_ai.connectors.gmail.mime import build_gmail_mime_message


def _parse(raw: str):  # type: ignore[no-untyped-def]
    padded = raw + ("=" * (-len(raw) % 4))
    return BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(padded))


def test_build_new_message_preserves_headers_bcc_and_body_alternatives() -> None:
    message = CanonicalEmail(
        mode="NEW",
        to=("person@example.com",),
        cc=("copy@example.com",),
        bcc=("hidden@example.com",),
        subject="A useful subject",
        text_body="Plain text body",
        html_body="<p>Plain <strong>text</strong> body</p>",
    )

    encoded = build_gmail_mime_message(
        message,
        sender="owner@example.com",
        rfc_message_id="<draft-123@eva.evaatyourservice.com>",
    )
    parsed = _parse(encoded.raw)

    assert encoded.thread_id is None
    assert parsed["From"] == "owner@example.com"
    assert parsed["To"] == "person@example.com"
    assert parsed["Cc"] == "copy@example.com"
    # Gmail needs Bcc in the pre-provider RFC message for delivery. Its sent copy may remove it.
    assert parsed["Bcc"] == "hidden@example.com"
    assert parsed["Subject"] == "A useful subject"
    assert parsed["Message-ID"] == "<draft-123@eva.evaatyourservice.com>"
    assert parsed["In-Reply-To"] is None
    assert parsed["References"] is None
    assert parsed.get_body(preferencelist=("plain",)).get_content() == "Plain text body\r\n"
    assert parsed.get_body(preferencelist=("html",)).get_content() == (
        "<p>Plain <strong>text</strong> body</p>\r\n"
    )


def test_build_reply_preserves_exact_thread_and_rfc_reply_context() -> None:
    message = CanonicalEmail(
        mode="REPLY",
        to=("interviewer@example.com",),
        subject="Re: Scaler interview",
        text_body="Thank you. I confirm.",
        thread_id="gmail-thread-123",
        in_reply_to="<original@example.com>",
        references=("<earlier@example.com>", "<original@example.com>"),
    )

    encoded = build_gmail_mime_message(
        message,
        sender="owner@example.com",
        rfc_message_id="<reply-456@eva.evaatyourservice.com>",
    )
    parsed = _parse(encoded.raw)

    assert encoded.thread_id == "gmail-thread-123"
    assert parsed["Subject"] == "Re: Scaler interview"
    assert parsed["Message-ID"] == "<reply-456@eva.evaatyourservice.com>"
    assert parsed["In-Reply-To"] == "<original@example.com>"
    assert parsed["References"] == "<earlier@example.com> <original@example.com>"
    assert parsed.get_content() == "Thank you. I confirm.\r\n"


def test_building_same_message_with_same_application_id_is_deterministic() -> None:
    message = CanonicalEmail(
        mode="NEW",
        to=("person@example.com",),
        subject="Deterministic",
        text_body="Same bytes every time",
    )

    first = build_gmail_mime_message(
        message,
        sender="owner@example.com",
        rfc_message_id="<stable@eva.evaatyourservice.com>",
    )
    second = build_gmail_mime_message(
        message,
        sender="owner@example.com",
        rfc_message_id="<stable@eva.evaatyourservice.com>",
    )

    assert first == second
