from collections.abc import Mapping
from uuid import UUID

from eva_ai.agent.errors import AgentPermanentError
from eva_ai.agent.types import GmailMessageEvidence, GmailSearchEvidence, GmailThreadEvidence
from eva_ai.connectors.gmail.contracts import GmailClient, MessageUnavailable
from eva_ai.connectors.gmail.normalizer import normalize_message
from eva_ai.connectors.types import ConnectorRecord, ConnectorStatus


class ScopedGmailInvestigationReader:
    def __init__(
        self,
        client: GmailClient,
        *,
        connector_id: UUID,
        user_id: UUID,
        workspace_id: UUID,
        thread_id: str,
        thread_message_limit: int,
        search_result_limit: int,
        body_max_chars: int,
    ) -> None:
        self._client = client
        self._connector = ConnectorRecord(
            id=connector_id,
            user_id=user_id,
            workspace_id=workspace_id,
            provider="gmail",
            account_identity="scoped-agent-read",
            granted_scopes=(),
            status=ConnectorStatus.ACTIVE,
            secret_reference=None,
            connected_at=None,
        )
        self._thread_id = thread_id
        self._thread_message_limit = thread_message_limit
        self._search_result_limit = search_result_limit
        self._body_max_chars = body_max_chars

    async def read_thread(self) -> GmailThreadEvidence:
        raw_thread = await self._client.get_thread(self._thread_id)
        raw_messages = raw_thread.get("messages")
        if not isinstance(raw_messages, list):
            raise AgentPermanentError("Gmail thread response is invalid")
        evidence = sorted(
            (
                _evidence(raw, self._connector, self._body_max_chars)
                for raw in raw_messages
                if isinstance(raw, Mapping)
            ),
            key=lambda message: (message.internal_date, message.message_id),
        )
        selected = evidence[-self._thread_message_limit :]
        return GmailThreadEvidence(
            thread_id=self._thread_id,
            messages=tuple(selected),
            truncated=len(evidence) > len(selected),
        )

    async def search(self, query: str) -> GmailSearchEvidence:
        normalized_query = " ".join(query.split())
        if not normalized_query or len(normalized_query) > 500:
            raise AgentPermanentError("Gmail search query is invalid")
        page = await self._client.list_message_ids(
            normalized_query,
            None,
            self._search_result_limit,
        )
        evidence: list[GmailMessageEvidence] = []
        for message_id in page.message_ids[: self._search_result_limit]:
            try:
                raw = await self._client.get_message(message_id)
            except MessageUnavailable:
                # Search results can race deletion. Skipping that result is safer than failing a
                # whole investigation or returning provider-controlled error details.
                continue
            evidence.append(_evidence(raw, self._connector, self._body_max_chars))
        evidence.sort(key=lambda message: (message.internal_date, message.message_id), reverse=True)
        return GmailSearchEvidence(
            messages=tuple(evidence),
            truncated=page.next_page_token is not None
            or len(page.message_ids) > self._search_result_limit,
        )


def _evidence(
    raw: Mapping[str, object], connector: ConnectorRecord, body_max_chars: int
) -> GmailMessageEvidence:
    event = normalize_message(raw, connector, "agent-read")
    headers = event.payload.get("headers")
    header_values = headers if isinstance(headers, dict) else {}
    labels = event.payload.get("label_ids")
    attachments = event.payload.get("attachments")
    return GmailMessageEvidence(
        message_id=_string(event.payload.get("message_id"))[:500],
        thread_id=_string(event.payload.get("thread_id"))[:500],
        internal_date=event.occurred_at,
        sender=_string(header_values.get("from"))[:500],
        recipients=_string(header_values.get("to"))[:1000],
        subject=_string(header_values.get("subject"))[:500],
        snippet=_string(event.payload.get("snippet"))[:1000],
        plain_text=_string(event.payload.get("plain_text"))[:body_max_chars],
        label_ids=tuple(value[:200] for value in labels if isinstance(value, str))
        if isinstance(labels, list)
        else (),
        attachments=tuple(value for value in attachments if isinstance(value, dict))[:50]
        if isinstance(attachments, list)
        else (),
    )


def _string(value: object) -> str:
    return value if isinstance(value, str) else ""
