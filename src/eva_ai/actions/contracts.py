from datetime import datetime
from typing import Protocol

from eva_ai.actions.types import (
    ActionClaimResult,
    ActionTaskRequest,
    AllowedActionCreation,
    NewActionProposal,
)


class ActionStore(Protocol):
    async def create_allowed_action(
        self,
        *,
        proposal: NewActionProposal,
        destination: str,
    ) -> AllowedActionCreation: ...

    async def claim_action(
        self,
        request: ActionTaskRequest,
        *,
        now: datetime,
        lease_seconds: int,
    ) -> ActionClaimResult: ...
