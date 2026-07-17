# Database modes and migration status

Restia V3 has one application/API policy boundary, one account identity model,
and a deterministic Alembic schema baseline. The runtime separately gates the
remaining local-only worker and sidecar state with `RESTIA_DATABASE_MODE`.

## `local-single` (default)

- Requires a SQLite `DATABASE_URL`.
- Keeps Restia's private, local-first deployment behavior.
- Runs Alembic on an empty database. A pre-Alembic Restia database receives one
  final compatibility bootstrap and is stamped only after the frozen baseline
  manifest verifies every table, security-critical columns, external-identity
  uniqueness, and append-only audit guards.
- Importing `core.database` alone does not create tables or run migrations.
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
The schema itself now compiles for PostgreSQL, but email tags/schedules,
Telegram offsets/link state, reminder outboxes, some preferences, worker
leadership, and attachment storage still have local SQLite/file authorities.
Enabling multiple replicas before those are leased and fenced in PostgreSQL
could duplicate sends or diverge state.

Supabase can provide managed PostgreSQL and optional JWT identity. Set the
public project origin in `RESTIA_SUPABASE_PROJECT_URL` (and, only when needed,
`RESTIA_SUPABASE_AUDIENCE`; default `authenticated`). Restia derives and pins
the issuer/JWKS URLs, accepts asymmetric RS256/ES256 access tokens only, and
requires an authenticated user to explicitly link the exact opaque subject to
an immutable Restia account. Browser/mobile clients still go through Restia's
FastAPI authorization and audit boundary; they never receive a service-role key
or query Restia tables directly.

The exchange endpoints accept the Supabase access token in a bounded JSON body:

- `POST /api/auth/external/supabase/link` requires an existing Restia session
  plus the current local password and, when enabled, a fresh Restia TOTP or
  recovery code. It links that exact verified subject to the session's account.
- `POST /api/auth/external/supabase/login` accepts only an already-linked
  subject, enforces the account's current Restia MFA policy, and returns the
  normal HttpOnly Restia session cookie. A password change disables existing
  external links until the user explicitly performs the step-up link again.

Neither endpoint auto-links by email, username, or token metadata.

## Migration status and legacy baseline

Install the core requirements, then inspect schema state without mutating it:

```bash
scripts/odysseus-db status --pretty
```

Fresh databases are created automatically by recording the immutable,
stamp-only `20260716_0001` foundation and then executing the explicit
`20260717_0002` schema. For an existing installation, normal application
startup first creates a private SQLite backup, runs the compatibility repair,
and verifies the frozen 0002 contract. Operators may inspect or explicitly
record an already-repaired schema with:

```bash
scripts/odysseus-db stamp-legacy --pretty
```

The stamp command verifies the frozen baseline manifest and security constraints
and refuses empty, partial, pre-unified-auth, or unknown-revision schemas. Use
normal Restia startup for an empty database; raw Alembic must begin from the
recorded 0001 boundary (`alembic upgrade 20260716_0001:head`). PostgreSQL
application startup stays blocked until the runtime-readiness constant is changed
together with the remaining distributed-state ports and contract tests.

Generate a Fernet key directly into an owner-only secret file, rather than
displaying it in the terminal or committing it to repository files:

```bash
umask 077
python -c "from cryptography.fernet import Fernet; import sys; sys.stdout.buffer.write(Fernet.generate_key())" > /secure/path/restia_fernet_key
```

Point `RESTIA_ENCRYPTION_KEY_FILE` at that file, or store the same value through
your deployment secret manager. Never commit it to `.env`.
