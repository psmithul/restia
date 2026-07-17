from __future__ import annotations

from datetime import datetime

from sqlalchemy import create_engine, text

from src.database_migrations import upgrade_schema


def test_fresh_sqlite_baseline_installs_and_updates_transcript_fts(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    try:
        upgrade_schema(engine)
        now = datetime.utcnow()
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO sessions "
                    "(id, name, endpoint_url, model, created_at, updated_at) "
                    "VALUES (:id, :name, :endpoint, :model, :created, :updated)"
                ),
                {
                    "id": "session-1",
                    "name": "FTS test",
                    "endpoint": "http://127.0.0.1",
                    "model": "test",
                    "created": now,
                    "updated": now,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO chat_messages (id, session_id, role, content) "
                    "VALUES ('message-1', 'session-1', 'user', 'orchid baseline')"
                )
            )
            assert connection.execute(
                text(
                    "SELECT message_id FROM chat_messages_fts "
                    "WHERE chat_messages_fts MATCH 'orchid'"
                )
            ).scalar_one() == "message-1"

            connection.execute(
                text(
                    "UPDATE chat_messages SET content='cedar replacement' "
                    "WHERE id='message-1'"
                )
            )
            assert connection.execute(
                text(
                    "SELECT message_id FROM chat_messages_fts "
                    "WHERE chat_messages_fts MATCH 'cedar'"
                )
            ).scalar_one() == "message-1"
            assert connection.execute(
                text(
                    "SELECT count(*) FROM chat_messages_fts "
                    "WHERE chat_messages_fts MATCH 'orchid'"
                )
            ).scalar_one() == 0
    finally:
        engine.dispose()
