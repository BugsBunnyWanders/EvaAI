from eva_ai.actions.recipients import (
    RecipientCandidate,
    RecipientResolutionStatus,
    resolve_recipient,
)


def test_explicit_email_address_resolves_without_history_candidates() -> None:
    result = resolve_recipient(" Person@Example.COM ", ())

    assert result.status is RecipientResolutionStatus.RESOLVED
    assert result.email_address == "person@example.com"
    assert result.clarification is None


def test_one_matching_history_candidate_resolves_named_person() -> None:
    result = resolve_recipient(
        "Jane Doe",
        (
            RecipientCandidate(
                display_name="Jane Doe",
                email_address="jane@example.com",
            ),
        ),
    )

    assert result.status is RecipientResolutionStatus.RESOLVED
    assert result.email_address == "jane@example.com"


def test_no_history_match_requires_an_address_clarification() -> None:
    result = resolve_recipient("Jane Doe", ())

    assert result.status is RecipientResolutionStatus.CLARIFICATION_REQUIRED
    assert result.email_address is None
    assert result.clarification == "What email address should I use for Jane Doe?"


def test_multiple_history_matches_never_guess() -> None:
    result = resolve_recipient(
        "Jane",
        (
            RecipientCandidate(display_name="Jane Doe", email_address="jane.d@example.com"),
            RecipientCandidate(display_name="Jane Roe", email_address="jane.r@example.com"),
        ),
    )

    assert result.status is RecipientResolutionStatus.CLARIFICATION_REQUIRED
    assert result.email_address is None
    assert result.clarification == (
        "I found multiple email addresses for Jane. Which address should I use?"
    )


def test_duplicate_candidate_addresses_count_as_one_match() -> None:
    result = resolve_recipient(
        "Jane Doe",
        (
            RecipientCandidate(display_name="Jane Doe", email_address="jane@example.com"),
            RecipientCandidate(display_name="JANE DOE", email_address="JANE@example.com"),
        ),
    )

    assert result.status is RecipientResolutionStatus.RESOLVED
    assert result.email_address == "jane@example.com"
