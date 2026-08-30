from eva_ai.events.errors import sanitize_error


def test_sanitize_error_never_persists_exception_text() -> None:
    stored = sanitize_error(RuntimeError("postgresql://eva:secret@db/eva?token=top-secret"))
    assert stored.error_type == "RuntimeError"
    assert stored.summary == "operation failed"
    assert "secret" not in stored.model_dump_json()
