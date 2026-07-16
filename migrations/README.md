# Restia schema migration foundation

This directory is intentionally a scaffold, not a claim that the current ORM
is portable to PostgreSQL.

The `20260716_0001` revision is **stamp-only**. Existing local SQLite installs
still receive the current idempotent bootstrap in `core.database.init_db()`,
called explicitly by production entrypoints. After that bootstrap succeeds,
`scripts/odysseus-db stamp-legacy` verifies a frozen set of sentinel tables and
records the baseline without running revision upgrade code.

Do not run `alembic upgrade head` on an empty database: the baseline refuses
that operation. Shared/PostgreSQL mode also refuses application startup until
the hand-written SQLite migrations have been replaced by reviewed,
deterministic Alembic revisions and `SHARED_SCHEMA_AUTHORITY_READY` is enabled
in the same release.

Useful diagnostics:

```bash
scripts/odysseus-db status --pretty
scripts/odysseus-db stamp-legacy --pretty
alembic current
alembic heads
```
