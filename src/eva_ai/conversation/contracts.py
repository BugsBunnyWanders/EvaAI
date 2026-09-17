from typing import Protocol

from eva_ai.agent.contracts import GmailInvestigationReader
from eva_ai.conversation.types import (
    ConversationAgentRequest,
    ConversationInvocationResult,
    DraftRevisionInvocationResult,
    DraftRevisionRequest,
)


class ConversationAgent(Protocol):
    async def respond(
        self,
        request: ConversationAgentRequest,
        reader: GmailInvestigationReader | None,
    ) -> ConversationInvocationResult: ...


class DraftRevisionAgent(Protocol):
    async def revise(self, request: DraftRevisionRequest) -> DraftRevisionInvocationResult: ...
