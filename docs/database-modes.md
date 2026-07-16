# Database modes and migration status

Restia V3 has one application/API policy boundary and one account identity
model, but it does **not** yet claim that the legacy schema is PostgreSQL-ready.
The runtime makes that distinction explicit with `RESTIA_DATABASE_MODE`.

## `local-single` (default)

- Requires a SQLite `DATABASE_URL`.
- Keeps Restia's private, local-first deployment behavior.
- Runs the existing idempotent schema bootstrap explicitly from each
  database-backed production entrypoint. Importing `core.database` alone does
  not create tables or run migrations.
- May use the auto-generated `data/.app_key`, or an explicitly supplied key.

## `shared` (guarded, not available yet)

Shared mode is the future cross-interface/multi-device deployment path. Its
validator requires all of the following before it even reaches the schema
authority gate:

- a PostgreSQL `DATABASE_URL`;
- `AUTH_ENABLED=true`;
- `LOCALHOST_BYPASS=false`;
- one stable Fernet key shared by every Restia process, supplied as either
  `RESTIA_ENCRYPTION_KEY` or `RESTIA_ENCRYPTION_KEY_FILE`.

Even with those requirements present, this release refuses shared-mode startup.
The current bootstrap still contains SQLite-specific `PRAGMA` and hand-written
migrations. Enabling PostgreSQL before deterministic Alembic revisions exist
would create partial or divergent schemas.

Supabase can later provide managed PostgreSQL and OIDC, but clients must still
go through Restia's FastAPI authorization/audit boundary. This foundation does
not enable direct browser access to Supabase tables.

## Migration status and legacy baseline

Install the core requirements, then inspect schema state without mutating it:

```bash
scripts/odysseus-db status --pretty
```

The first Alembic revision is deliberately stamp-only. After the current local
bootstrap has successfully created every V3 foundation table, record the
baseline with:

```bash
scripts/odysseus-db stamp-legacy --pretty
```

The stamp command verifies a frozen list of sentinel tables and refuses empty,
partial, or already-mismatched schemas. Do not run `alembic upgrade head` on an
empty database; the baseline cannot create one. PostgreSQL stays blocked until
the legacy migrations are replaced and the schema-readiness constant is changed
in the same reviewed release.

Generate a Fernet key directly into an owner-only secret file, rather than
displaying it in the terminal or committing it to repository files:

```bash
umask 077
python -c "from cryptography.fernet import Fernet; import sys; sys.stdout.buffer.write(Fernet.generate_key())" > /secure/path/restia_fernet_key
```

Point `RESTIA_ENCRYPTION_KEY_FILE` at that file, or store the same value through
your deployment secret manager. Never commit it to `.env`.
