from collections.abc import Mapping

import pytest

from eva_ai.actions.canonical import CanonicalEmail
from eva_ai.connectors.gmail.actions import GmailDraftActionAdapter
from eva_ai.connectors.gmail.contracts import GmailDraftResult, GmailSendResult


class FakeActionClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.created: tuple[str, str | None] | None = None
        self.updated: tuple[str, str, str | None] | None = None

    async def create_draft(self, raw: str, thread_id: str | None) -> GmailDraftResult:
        self.created = (raw, thread_id)
        self.calls.append(("create", (raw, thread_id)))
        return GmailDraftResult("draft-1", "message-1", thread_id)

    async def update_draft(
        self, draft_id: str, raw: str, thread_id: str | None
    ) -> GmailDraftResult:
        self.updated = (draft_id, raw, thread_id)
        self.calls.append(("update", (draft_id, raw, thread_id)))
        return GmailDraftResult(draft_id, "message-2", thread_id)

    async def delete_draft(self, draft_id: str) -> None:
        self.calls.append(("delete", draft_id))

    async def send_draft(self, draft_id: str) -> GmailSendResult:
        self.calls.append(("send", draft_id))
        return GmailSendResult("sent-message-1", "sent-thread-1")

    async def get_draft(self, draft_id: str) -> Mapping[str, object]:
        self.calls.append(("get", draft_id))
        return {"id": draft_id}

    async def close(self) -> None:
        self.calls.append(("close", None))


@pytest.mark.asyncio
async def test_adapter_builds_mime_and_delegates_only_draft_operations() -> None:
    client = FakeActionClient()
    adapter = GmailDraftActionAdapter(client, sender="owner@example.com")
    message = CanonicalEmail(
        mode="NEW",
        to=("person@example.com",),
        subject="Hello",
        text_body="Body",
    )

    created = await adapter.create_draft(
        message,
        rfc_message_id="<create@eva.evaatyourservice.com>",
    )
    updated = await adapter.update_draft(
        "draft-1",
        message,
        rfc_message_id="<update@eva.evaatyourservice.com>",
    )
    loaded = await adapter.get_draft("draft-1")
    sent = await adapter.send_draft("draft-1")
    await adapter.delete_draft("draft-1")
    await adapter.close()

    assert created.message_id == "message-1"
    assert updated.message_id == "message-2"
    assert loaded == {"id": "draft-1"}
    assert sent == GmailSendResult("sent-message-1", "sent-thread-1")
    assert [name for name, _ in client.calls] == [
        "create",
        "update",
        "get",
        "send",
        "delete",
        "close",
    ]
    assert client.created is not None
    assert client.updated is not None
    create_raw, create_thread = client.created
    update_draft_id, update_raw, update_thread = client.updated
    assert isinstance(create_raw, str) and create_raw
    assert create_thread is None
    assert update_draft_id == "draft-1"
    assert isinstance(update_raw, str) and update_raw
    assert update_thread is None
