import pytest

from eva_ai.situations import (
    InvalidSituationTransitionError,
    SituationLifecycle,
    validate_situation_transition,
)

ALLOWED_TRANSITIONS = {
    SituationLifecycle.OPEN: {
        SituationLifecycle.ACTIVE,
        SituationLifecycle.WAITING_USER,
        SituationLifecycle.WAITING_EXTERNAL,
        SituationLifecycle.RESOLVED,
        SituationLifecycle.ABANDONED,
    },
    SituationLifecycle.ACTIVE: {
        SituationLifecycle.WAITING_USER,
        SituationLifecycle.WAITING_EXTERNAL,
        SituationLifecycle.RESOLVED,
        SituationLifecycle.ABANDONED,
    },
    SituationLifecycle.WAITING_USER: {
        SituationLifecycle.ACTIVE,
        SituationLifecycle.WAITING_EXTERNAL,
        SituationLifecycle.RESOLVED,
        SituationLifecycle.ABANDONED,
    },
    SituationLifecycle.WAITING_EXTERNAL: {
        SituationLifecycle.ACTIVE,
        SituationLifecycle.WAITING_USER,
        SituationLifecycle.RESOLVED,
        SituationLifecycle.ABANDONED,
    },
    SituationLifecycle.RESOLVED: set(),
    SituationLifecycle.ABANDONED: set(),
}


@pytest.mark.parametrize("current", list(SituationLifecycle))
@pytest.mark.parametrize("requested", list(SituationLifecycle))
def test_situation_transition_matrix(
    current: SituationLifecycle, requested: SituationLifecycle
) -> None:
    if requested == current or requested in ALLOWED_TRANSITIONS[current]:
        validate_situation_transition(current, requested)
    else:
        with pytest.raises(
            InvalidSituationTransitionError,
            match="Situation lifecycle transition is invalid",
        ):
            validate_situation_transition(current, requested)
