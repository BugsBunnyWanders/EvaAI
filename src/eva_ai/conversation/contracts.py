from typing import Protocol

from eva_ai.agent.contracts import GmailInvestigationReader
from eva_ai.conversation.types import ConversationAgentRequest, ConversationInvocationResult


class ConversationAgent(Protocol):
    async def respond(
        self,
        request: ConversationAgentRequest,
        reader: GmailInvestigationReader | None,
    ) -> ConversationInvocationResult: ...
