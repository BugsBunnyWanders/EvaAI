from collections.abc import Mapping

from eva_ai.actions.canonical import CanonicalEmail
from eva_ai.connectors.gmail.contracts import (
    GmailActionClient,
    GmailDraftResult,
    GmailSendResult,
)
from eva_ai.connectors.gmail.mime import build_gmail_mime_message


class GmailDraftActionAdapter:
    """Application-facing adapter restricted to Eva-managed Gmail drafts."""

    def __init__(self, client: GmailActionClient, *, sender: str) -> None:
        self._client = client
        self._sender = sender

    async def create_draft(
        self,
        message: CanonicalEmail,
        *,
        rfc_message_id: str,
    ) -> GmailDraftResult:
        mime = build_gmail_mime_message(
            message,
            sender=self._sender,
            rfc_message_id=rfc_message_id,
        )
        return await self._client.create_draft(mime.raw, mime.thread_id)

    async def update_draft(
        self,
        draft_id: str,
        message: CanonicalEmail,
        *,
        rfc_message_id: str,
    ) -> GmailDraftResult:
        mime = build_gmail_mime_message(
            message,
            sender=self._sender,
            rfc_message_id=rfc_message_id,
        )
        return await self._client.update_draft(draft_id, mime.raw, mime.thread_id)

    async def delete_draft(self, draft_id: str) -> None:
        await self._client.delete_draft(draft_id)

    async def send_draft(self, draft_id: str) -> GmailSendResult:
        return await self._client.send_draft(draft_id)

    async def get_draft(self, draft_id: str) -> Mapping[str, object]:
        return await self._client.get_draft(draft_id)

    async def close(self) -> None:
        await self._client.close()
