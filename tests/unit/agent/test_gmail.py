import base64
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import cast
from uuid import uuid7

from eva_ai.agent.gmail import ScopedGmailInvestigationReader
from eva_ai.connectors.gmail.contracts import GmailClient as GmailClientProtocol
from eva_ai.connectors.gmail.contracts import MessageListPage


class GmailClient:
    async def get_thread(self, thread_id: str) -> Mapping[str, object]:
        return {
            "messages": [_message("m1", thread_id, "older"), _message("m2", thread_id, "newer")]
        }

    async def list_message_ids(
        self, query: str, page_token: str | None, max_results: int = 100
    ) -> MessageListPage:
        assert query == "from:person@example.com"
        assert page_token is None
        assert max_results == 1
        return MessageListPage(message_ids=("m2",), next_page_token="more")

    async def get_message(self, message_id: str) -> Mapping[str, object]:
        return _message(message_id, "thread-1", "search")


def _message(message_id: str, thread_id: str, body: str) -> dict[str, object]:
    encoded = base64.urlsafe_b64encode(body.encode()).decode().rstrip("=")
    return {
        "id": message_id,
        "threadId": thread_id,
        "internalDate": str(int(datetime(2026, 9, 7, tzinfo=UTC).timestamp() * 1000)),
        "snippet": body,
        "labelIds": ["INBOX"],
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "From", "value": "Person <person@example.com>"},
                {"name": "To", "value": "owner@example.com"},
                {"name": "Subject", "value": "Meeting"},
            ],
            "body": {"data": encoded, "size": len(body)},
        },
    }


async def test_reader_enforces_linked_thread_and_bounds_search() -> None:
    reader = ScopedGmailInvestigationReader(
        cast(GmailClientProtocol, GmailClient()),
        connector_id=uuid7(),
        user_id=uuid7(),
        workspace_id=uuid7(),
        thread_id="thread-1",
        thread_message_limit=1,
        search_result_limit=1,
        body_max_chars=3,
    )

    thread = await reader.read_thread()
    search = await reader.search("  from:person@example.com  ")

    assert thread.thread_id == "thread-1"
    assert len(thread.messages) == 1 and thread.truncated is True
    assert thread.messages[0].plain_text == "new"
    assert len(search.messages) == 1 and search.truncated is True
    assert search.messages[0].plain_text == "sea"
