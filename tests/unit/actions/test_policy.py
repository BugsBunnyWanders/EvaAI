import pytest

from eva_ai.actions.policy import ActionPolicyContext, ActionPolicyEngine
from eva_ai.actions.types import GmailActionCapability, PolicyDecision


@pytest.mark.parametrize(
    ("capability", "context"),
    [
        (
            GmailActionCapability.CREATE_DRAFT,
            ActionPolicyContext(scoped_connector=True),
        ),
        (
            GmailActionCapability.UPDATE_DRAFT,
            ActionPolicyContext(scoped_connector=True, managed_draft=True),
        ),
        (
            GmailActionCapability.DELETE_DRAFT,
            ActionPolicyContext(
                scoped_connector=True,
                managed_draft=True,
                authenticated_discard=True,
            ),
        ),
    ],
)
def test_safe_draft_operations_are_allowed_only_with_trusted_context(
    capability: GmailActionCapability,
    context: ActionPolicyContext,
) -> None:
    assert ActionPolicyEngine().evaluate(capability, context) is PolicyDecision.ALLOW


def test_send_always_requires_exact_approval() -> None:
    engine = ActionPolicyEngine()

    assert (
        engine.evaluate(
            GmailActionCapability.SEND_DRAFT,
            ActionPolicyContext(scoped_connector=True, managed_draft=True),
        )
        is PolicyDecision.REQUIRE_APPROVAL
    )
    assert (
        engine.evaluate(
            GmailActionCapability.SEND_DRAFT,
            ActionPolicyContext(
                scoped_connector=True,
                managed_draft=True,
                exact_approval=True,
            ),
        )
        is PolicyDecision.REQUIRE_APPROVAL
    )


@pytest.mark.parametrize(
    ("capability", "context"),
    [
        (GmailActionCapability.CREATE_DRAFT, ActionPolicyContext()),
        (
            GmailActionCapability.UPDATE_DRAFT,
            ActionPolicyContext(scoped_connector=True),
        ),
        (
            GmailActionCapability.SEND_DRAFT,
            ActionPolicyContext(scoped_connector=True),
        ),
        (
            GmailActionCapability.DELETE_DRAFT,
            ActionPolicyContext(scoped_connector=True, managed_draft=True),
        ),
    ],
)
def test_policy_denies_missing_trusted_preconditions(
    capability: GmailActionCapability,
    context: ActionPolicyContext,
) -> None:
    assert ActionPolicyEngine().evaluate(capability, context) is PolicyDecision.DENY


def test_unknown_capability_is_not_constructible() -> None:
    with pytest.raises(ValueError, match="not-a-capability"):
        GmailActionCapability("not-a-capability")
