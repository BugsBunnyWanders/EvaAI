from dataclasses import dataclass

from eva_ai.actions.types import GmailActionCapability, PolicyDecision


@dataclass(frozen=True, slots=True)
class ActionPolicyContext:
    scoped_connector: bool = False
    managed_draft: bool = False
    authenticated_discard: bool = False
    exact_approval: bool = False


class ActionPolicyEngine:
    """Evaluate only trusted application state against the closed action registry."""

    def evaluate(
        self,
        capability: GmailActionCapability,
        context: ActionPolicyContext,
    ) -> PolicyDecision:
        if not context.scoped_connector:
            return PolicyDecision.DENY
        match capability:
            case GmailActionCapability.CREATE_DRAFT:
                return PolicyDecision.ALLOW
            case GmailActionCapability.UPDATE_DRAFT:
                return PolicyDecision.ALLOW if context.managed_draft else PolicyDecision.DENY
            case GmailActionCapability.SEND_DRAFT:
                return (
                    PolicyDecision.REQUIRE_APPROVAL
                    if context.managed_draft
                    else PolicyDecision.DENY
                )
            case GmailActionCapability.DELETE_DRAFT:
                return (
                    PolicyDecision.ALLOW
                    if context.managed_draft and context.authenticated_discard
                    else PolicyDecision.DENY
                )
