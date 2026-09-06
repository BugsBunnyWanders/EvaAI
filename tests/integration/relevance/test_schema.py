import pytest
from sqlalchemy import inspect

from eva_ai.db import Database


@pytest.mark.integration
async def test_relevance_schema_has_scoped_history_and_current_uniqueness(
    database: Database,
) -> None:
    async with database.engine.connect() as connection:
        table_names, signal_uniques, attempt_uniques, signal_indexes = await connection.run_sync(
            lambda sync_connection: (
                inspect(sync_connection).get_table_names(),
                inspect(sync_connection).get_unique_constraints("signals"),
                inspect(sync_connection).get_unique_constraints("relevance_evaluation_attempts"),
                inspect(sync_connection).get_indexes("signals"),
            )
        )

    assert {"signals", "signal_goals", "relevance_evaluation_attempts"} <= set(table_names)
    assert "uq_signals_scope_evaluation" in {item["name"] for item in signal_uniques}
    assert "uq_relevance_attempt_scope" in {item["name"] for item in attempt_uniques}
    assert "uq_signals_current_relevance" in {item["name"] for item in signal_indexes}
