# Task 11 live decoder fix report

Date: 2026-08-30

## RED test and exact failure

Added `test_decode_notification_normalizes_live_gmail_numeric_history_id` with the production-shaped payload `{"emailAddress":"mail@example.com","historyId":12345}`. Before the implementation change:

```text
$ uv run pytest tests/unit/connectors/gmail/test_notification.py::test_decode_notification_normalizes_live_gmail_numeric_history_id
============================= test session starts ==============================
collected 1 item
...
FAILED tests/unit/connectors/gmail/test_notification.py::test_decode_notification_normalizes_live_gmail_numeric_history_id
E   eva_ai.connectors.gmail.contracts.InvalidNotification: invalid Gmail notification
1 failed in 0.02s
```

The failure occurred at `decode_notification()`'s string-only `historyId` check, confirming the regression test exercised the known live-delivery root cause.

## Implementation summary

- Added `_normalize_history_id()` at the decoder boundary.
- Preserved ASCII decimal-string validation and internal string cursors.
- Accepted non-negative JSON integers and normalized them with `str()`.
- Explicitly rejected booleans, negative integers, fractional numbers, non-decimal strings, missing fields, malformed JSON, and other invalid shapes.
- Added regression coverage for the live integer and invalid numeric forms.

## Tests, commands, and output

- `uv run pytest tests/unit/connectors/gmail/test_notification.py` — `11 passed in 0.01s`.
- `uv run pytest tests/unit/connectors/gmail` — `119 passed in 0.48s`.
- `uv run pytest` — `303 passed in 2.59s`.
- `uv run ruff format --check src migrations tests` — `89 files already formatted`.
- `uv run ruff check .` — `All checks passed!`.
- `uv run mypy src migrations tests` — `Success: no issues found in 89 source files`.
- `git diff --check` — no whitespace errors.

## Files changed

- `src/eva_ai/connectors/gmail/notification.py`
- `tests/unit/connectors/gmail/test_notification.py`
- `.superpowers/sdd/2026-08-30-milestone-2-gmail-ingestion/task-11-live-decoder-fix-report.md`

## Commit

Commit message: `fix Gmail numeric history notification decoding`.

## Self-review

- The change is limited to notification decoding and its unit tests.
- `bool` is checked separately because Python booleans are `int` subclasses.
- No provider payloads, credentials, tokens, or `.secrets` contents were read or printed.
- Existing email normalization and decimal-string behavior are unchanged.

## Concerns

None identified. The decoder now accepts the live Gmail JSON integer representation while retaining the narrow existing contract for all other forms.
