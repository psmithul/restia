# Database modes and migration status

Restia V3 has one application/API policy boundary, one account identity model,
and a deterministic Alembic schema baseline. `RESTIA_DATABASE_MODE` selects a
private SQLite runtime or the reviewed PostgreSQL/shared-blob deployment.

## `local-single` (default)

- Requires a SQLite `DATABASE_URL`.
- Keeps Restia's private, local-first deployment behavior.
- Runs Alembic on an empty database. A pre-Alembic Restia database receives one
  final compatibility bootstrap and is stamped only after the frozen baseline
  manifest verifies every table, security-critical columns, external-identity
  uniqueness, and append-only audit guards.
- Importing `core.database` alone does not create tables or run migrations.
- May use the auto-generated `data/.app_key`, or an explicitly supplied key.

## `shared` (PostgreSQL multi-interface mode)

Shared mode is the cross-interface/multi-device deployment path. Its validator
requires all of the following before it reaches the schema authority gate:

- a PostgreSQL `DATABASE_URL` using the explicit psycopg 3 form
  `postgresql+psycopg://user:password@host/database` (change Supabase's
  displayed `postgresql://` prefix to this SQLAlchemy driver prefix);
- `AUTH_ENABLED=true`;
- `LOCALHOST_BYPASS=false`;
- one stable Fernet key shared by every Restia process, supplied as either
  `RESTIA_ENCRYPTION_KEY` or `RESTIA_ENCRYPTION_KEY_FILE`;
- `RESTIA_BLOB_STORE=shared-filesystem` and an absolute
  `RESTIA_BLOB_ROOT` mounted to the same durable filesystem on every API and
  worker replica.

Email/Telegram/reminder runtime state, profiles and cross-interface
preferences, upload metadata, and singleton-worker leadership have canonical
SQL authorities. Local email cache files contain only rebuildable connector/LLM
data and retained read-only import sources. Every shared replica may start the
same workers; database-time leases and monotonic fencing tokens select one
current holder for each role and allow bounded standby takeover.

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
`20260717_0002` schema followed by the additive `20260718_0003` Life OS
planning spine, `20260719_0004` contact authority, `20260720_0005` calendar
authority, `20260721_0006` Telegram identity, `20260722_0007` approved-email
outbound authority, `20260723_0008` finance authority, and
`20260724_0009` Telegram polling/delivery authority, followed by
`20260725_0010` reminder/browser delivery authority, `20260726_0011`
email-to-Life projection authority, `20260727_0012` email runtime authority,
`20260728_0013` profile/configuration authority, `20260729_0014` upload
metadata/blob-store authority, and `20260730_0015` database-fenced runtime
leadership. For an existing
installation, normal application
startup first creates a private SQLite backup, runs the compatibility repair,
encrypts legacy planning-item text plus private edge metadata and provenance
with the configured Fernet key, adopts legacy contacts and CardDAV credentials
into owner-scoped encrypted tables after creating byte-identical private
backups, and verifies the frozen baseline plus every reviewed head contract.
If more than one admin exists at first cutover, set
`RESTIA_CONTACTS_IMPORT_OWNER` explicitly; Restia never assigns global legacy
contacts to a sorted or arbitrary admin. Keep the same encryption key available
for rollback: the 0003
downgrade preflights every encrypted value and aborts before destructive DDL if
it cannot restore the V2 plaintext contract. Revision 0003 is deliberately
online-only; Alembic `--sql` generation fails rather than producing SQL that could stamp
plaintext data as migrated. Operators may inspect or explicitly record
an already-repaired schema with:

```bash
scripts/odysseus-db stamp-legacy --pretty
```

Revision 0004 refuses downgrade while any contact source, contact, delivery,
or import marker remains. Export and deliberately remove contact authority data
before downgrading; legacy backups do not contain post-cutover edits.

Revision 0005 backfills calendar ownership to immutable Account IDs, adds
optimistic event/configuration versions, and introduces encrypted calendar undo
and CalDAV delivery-outbox tables. Calendar event identity is owner-scoped and
planning-event links are calendar-scoped, allowing legitimate RFC UID reuse
across accounts. Ownerless legacy calendars are accepted only
when exactly one active Account exists. Missing or ambiguous mappings abort
before schema mutation. Downgrade refuses state that revision 0004 cannot
represent, and also preflights retained contact authority so a multi-revision
SQLite downgrade cannot stop after partially changing the schema head.

Revision 0006 moves Telegram chat principals, one-time link credentials, and
conversation bindings out of `settings.json`. Chat identifiers and session
bindings are encrypted; bot-scoped keyed digests support deterministic lookup;
and every link resolves to immutable `Account.id`. Legacy maps are accepted
only by the transactional, verified import path and remain untouched as a
recovery source. Runtime readers never fall back to them. Replacing or
disconnecting a bot revokes its SQL principals, and relinking a chat to another
account invalidates its prior conversation. Revision 0009 moves polling
offsets, poison-update resolution, inbound claims, and reply delivery into the
same configured database.

Revision 0009 gives each bot one expiring, monotonic database-fenced polling
lease and cursor. Poison attempts and safe dead-letter metadata commit with
cursor resolution; inbound processing and reply delivery use separate expiring
claim digests keyed to the immutable first-claim Account ID. Telegram reply
delivery is honestly at-least-once because the Bot API has no idempotency key:
a crash after provider acceptance but before the completion commit may replay
the stored reply. The old JSON/SQLite sidecars are bounded, exact-bot import
sources only and are never runtime fallbacks after the import marker commits.

Revision 0014 makes chat-upload metadata canonical encrypted,
`Account.id`-owned SQL state. Owner/content idempotency uses a keyed digest;
retention uses optimistic versions and tombstones. Existing `uploads.json`
files are bounded, read-only import sources and remain untouched for recovery.
Project and chat bytes use one validated filesystem contract: local installs
keep their existing directories, while shared deployments fail closed unless
every replica is given the explicit shared mount described above. SQL rows are
the only runtime lookup authority, so unindexed bytes are never discovered by
walking the shared filesystem.

For an existing local install, copy the contents of `data/uploads` to the
shared root's `uploads` namespace and `data/project_files` to its
`project_files` namespace before the first shared start. Keep `uploads.json`
beside the copied chat blobs. Its historical absolute paths are remapped only
through their validated `YYYY/MM/DD/<upload-id>` suffix; the importer never
reads bytes from an old path outside the configured shared root.

The stamp command verifies the frozen baseline manifest and security constraints
and refuses empty, partial, pre-unified-auth, or unknown-revision schemas. Use
normal Restia startup for an empty database; raw Alembic must begin from the
recorded 0001 boundary (`alembic upgrade 20260716_0001:head`). PostgreSQL
application startup validates the current reviewed revision before serving.

## PostgreSQL integration gate

The driver is a core dependency, but installing it is not evidence that the
schema works on PostgreSQL. Point Restia at a PostgreSQL database for which the
configured user may create and drop a schema, provide the normal shared-mode
security settings above, then run:

```bash
scripts/odysseus-db shared-gate --migration-smoke --pretty
```

This creates a random `restia_gate_*` schema, runs the complete Alembic chain,
validates the reviewed head contract, and drops that schema in a `finally`
cleanup. It does not touch the database's existing Restia schema. The command's
`passed` field and exit code cover this PostgreSQL migration smoke only;
`runtime_ready` is true only when the schema contract passes and the reviewed
machine-readable blocker list is empty. Without
`--migration-smoke`, the command is read-only and checks the configured default
schema plus full runtime readiness.

The blocker codes are stable, machine-readable release gates. The reviewed V3
runtime currently has no blockers; any future local-only authority must add one
in the same change and shared startup will fail closed.

Profile settings, UI preferences, feature choices, and user-created
integrations are now canonical `Account.id`-owned SQL state shared by browser,
API-token, CLI, Telegram, and trusted internal adapters. Private values are
encrypted, writes use optimistic versions plus keyed idempotency digests, and
deletes are tombstones. Deployment authority such as database credentials,
the application encryption key, auth/session secrets, and process policy stays
environment-only and is rejected at the profile boundary. Retained
`settings.json`, `user_prefs.json`, `features.json`, and `integrations.json`
files are bounded, content-addressed, non-destructive import sources only; they
are never runtime fallback writers.

Telegram chat links, per-chat conversation bindings, Bot API offsets, poison
resolution, inbound idempotency claims, and reply-delivery fencing are now SQL
authority. Polling has its own `RESTIA_INPROCESS_TELEGRAM` lifecycle and remains
independent from ScheduledTask and `RESTIA_INPROCESS_TASKS`.

Reminder occurrence claims, cancel/rearm barriers, and browser notification
acknowledgements are also canonical `Account.id`-owned SQL state. Browser
payloads and linked claim tokens are encrypted, dedupe keys use keyed digests,
and startup adopts the retained V2 SQLite sidecars through bounded,
content-addressed import markers. The sidecars are no longer runtime fallback
authorities.

Email-to-Life projection delivery is also canonical `Account.id`-owned SQL
state. Subjects and RFC Message-ID/thread headers exist only inside the
encrypted projection payload; leases use digest-only tokens plus optimistic
versions. The local email index remains a rebuildable cache, so its commit and
the SQL enqueue are not falsely described as atomic. A bounded, idempotent,
read-only backfill of committed cache rows closes that crash window. New cache
rows retain an encrypted immutable recovery envelope so RFC thread evidence is
not degraded in the gap; import progress is also encrypted and the source
sidecar is never deleted.

Email tag/spam state, automation rules, manual schedules, and per-message
automation idempotency are canonical `Account.id`-owned SQL state. Private
message/location identity is stored as keyed digests and payloads are
encrypted. Manual SMTP delivery and automation operations commit database-time
claims before side effects, then fence completion with claim digests plus
optimistic versions. SMTP completion is honestly at-least-once: a crash after
provider acceptance but before completion can replay after lease expiry.
Retained tag/schedule/calendar/event sidecar tables are imported through a
bounded query-only connection and are never mutated. Only rebuildable IMAP
indexes/body metadata and LLM summary/reply/translation/signature caches remain
local.

Human-approved agent email continues to use its immutable owner-scoped SQL
outbox. The in-process email loop and `scripts/odysseus-mail poll-scheduled`
invoke the same delivery workers, while shared replicas elect one
`email-poller` leader through a 45-second database lease checked every 10
seconds. Shutdown cancels and awaits the leader loop so its lease is explicitly
released and a standby can take over.

Generate a Fernet key directly into an owner-only secret file, rather than
displaying it in the terminal or committing it to repository files:

```bash
umask 077
python -c "from cryptography.fernet import Fernet; import sys; sys.stdout.buffer.write(Fernet.generate_key())" > /secure/path/restia_fernet_key
```

Point `RESTIA_ENCRYPTION_KEY_FILE` at that file, or store the same value through
your deployment secret manager. Never commit it to `.env`.
