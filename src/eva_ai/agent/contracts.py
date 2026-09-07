from typing import Protocol

from eva_ai.agent.types import (
    AgentInvestigationResult,
    AgentInvocationRequest,
    AgentUsage,
    GmailSearchEvidence,
    GmailThreadEvidence,
    ToolCallAudit,
)


class GmailInvestigationReader(Protocol):
    async def read_thread(self) -> GmailThreadEvidence: ...

    async def search(self, query: str) -> GmailSearchEvidence: ...


class AgentInvocationResult(Protocol):
    @property
    def result(self) -> AgentInvestigationResult: ...

    @property
    def provider_response_id(self) -> str | None: ...

    @property
    def usage(self) -> AgentUsage: ...

    @property
    def tool_audit(self) -> tuple[ToolCallAudit, ...]: ...


class InvestigationAgent(Protocol):
    async def investigate(
        self,
        request: AgentInvocationRequest,
        reader: GmailInvestigationReader,
    ) -> AgentInvocationResult: ...
