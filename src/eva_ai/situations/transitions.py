from eva_ai.situations.errors import InvalidSituationTransitionError
from eva_ai.situations.types import SituationLifecycle

_ALLOWED_TRANSITIONS: dict[SituationLifecycle, frozenset[SituationLifecycle]] = {
    SituationLifecycle.OPEN: frozenset(
        {
            SituationLifecycle.ACTIVE,
            SituationLifecycle.WAITING_USER,
            SituationLifecycle.WAITING_EXTERNAL,
            SituationLifecycle.RESOLVED,
            SituationLifecycle.ABANDONED,
        }
    ),
    SituationLifecycle.ACTIVE: frozenset(
        {
            SituationLifecycle.WAITING_USER,
            SituationLifecycle.WAITING_EXTERNAL,
            SituationLifecycle.RESOLVED,
            SituationLifecycle.ABANDONED,
        }
    ),
    SituationLifecycle.WAITING_USER: frozenset(
        {
            SituationLifecycle.ACTIVE,
            SituationLifecycle.WAITING_EXTERNAL,
            SituationLifecycle.RESOLVED,
            SituationLifecycle.ABANDONED,
        }
    ),
    SituationLifecycle.WAITING_EXTERNAL: frozenset(
        {
            SituationLifecycle.ACTIVE,
            SituationLifecycle.WAITING_USER,
            SituationLifecycle.RESOLVED,
            SituationLifecycle.ABANDONED,
        }
    ),
    # Terminal Situations stay closed so a later Event cannot silently revive old context.
    SituationLifecycle.RESOLVED: frozenset(),
    SituationLifecycle.ABANDONED: frozenset(),
}


def validate_situation_transition(
    current: SituationLifecycle,
    requested: SituationLifecycle,
) -> None:
    if requested == current:
        return
    if requested not in _ALLOWED_TRANSITIONS[current]:
        raise InvalidSituationTransitionError
