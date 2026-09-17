import base64
import hashlib
from dataclasses import dataclass
from email.message import EmailMessage
from email.policy import SMTP

from eva_ai.actions.canonical import CanonicalEmail


@dataclass(frozen=True, slots=True)
class GmailMimeMessage:
    raw: str
    thread_id: str | None


def build_gmail_mime_message(
    message: CanonicalEmail,
    *,
    sender: str,
    rfc_message_id: str,
) -> GmailMimeMessage:
    """Build the exact RFC message that Gmail will store or deliver."""
    mime = EmailMessage(policy=SMTP)
    mime["From"] = sender
    if message.to:
        mime["To"] = ", ".join(message.to)
    if message.cc:
        mime["Cc"] = ", ".join(message.cc)
    if message.bcc:
        # Gmail removes Bcc from the sent copy, but needs it in this upload for delivery.
        mime["Bcc"] = ", ".join(message.bcc)
    mime["Subject"] = message.subject
    mime["Message-ID"] = rfc_message_id
    if message.mode == "REPLY":
        assert message.in_reply_to is not None
        mime["In-Reply-To"] = message.in_reply_to
        references = message.references or (message.in_reply_to,)
        mime["References"] = " ".join(references)

    mime.set_content(message.text_body, charset="utf-8")
    if message.html_body is not None:
        mime.add_alternative(message.html_body, subtype="html", charset="utf-8")
        # EmailMessage otherwise chooses a random multipart boundary during serialization.
        boundary_input = f"{sender}\0{rfc_message_id}\0{message.model_dump_json()}"
        boundary_hash = hashlib.sha256(boundary_input.encode("utf-8")).hexdigest()[:32]
        mime.set_boundary(f"eva-{boundary_hash}")

    encoded = base64.urlsafe_b64encode(mime.as_bytes(policy=SMTP)).decode("ascii").rstrip("=")
    return GmailMimeMessage(raw=encoded, thread_id=message.thread_id)
