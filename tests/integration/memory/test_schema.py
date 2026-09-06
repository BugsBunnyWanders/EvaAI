import pytest
from sqlalchemy import inspect, text

from eva_ai.db import Database


@pytest.mark.integration
async def test_memory_schema_has_scoped_constraints_and_vector_column(database: Database) -> None:
    async with database.engine.connect() as connection:
        tables, fact_indexes, fact_uniques, episode_uniques = await connection.run_sync(
            lambda sync_connection: (
                inspect(sync_connection).get_table_names(),
                inspect(sync_connection).get_indexes("memory_facts"),
                inspect(sync_connection).get_unique_constraints("memory_facts"),
                inspect(sync_connection).get_unique_constraints("episodic_memories"),
            )
        )
    async with database.session() as session:
        vector_type = await session.scalar(
            text(
                "SELECT format_type(a.atttypid, a.atttypmod) "
                "FROM pg_attribute a "
                "JOIN pg_class c ON c.oid = a.attrelid "
                "WHERE c.relname = 'episodic_memories' AND a.attname = 'embedding'"
            )
        )

    assert {"memory_facts", "episodic_memories", "episodic_memory_goals"} <= set(tables)
    assert {
        "uq_memory_facts_active_workspace_slot",
        "uq_memory_facts_active_goal_slot",
        "uq_memory_facts_active_situation_slot",
    } <= {item["name"] for item in fact_indexes}
    assert "uq_memory_facts_scope_idempotency" in {item["name"] for item in fact_uniques}
    assert "uq_episodic_memories_scope_idempotency" in {item["name"] for item in episode_uniques}
    assert vector_type == "vector(1536)"
