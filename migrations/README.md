# Restia schema migration foundation

This directory contains Restia's frozen, deterministic schema baseline.

The pushed `20260716_0001` revision remains immutable and stamp-only.
`20260717_0002` creates the full schema on an empty database using explicit
Alembic operations and does not import live ORM metadata. Empty databases stamp
0001 without executing its guard, then upgrade through 0002 and the additive
`20260718_0003` Life OS planning-spine and `20260719_0004` owner-scoped contact
authority revisions, followed by the online-only `20260720_0005` calendar
authority revision and `20260721_0006` Telegram identity authority. Existing local
SQLite installs receive a backed-up compatibility repair before
`scripts/odysseus-db stamp-legacy` verifies every baseline table, critical
security columns and constraints, issuer-qualified identity uniqueness, and
append-only audit/version guards. Only then does it record the current head
without replaying creation DDL over existing tables.

Revision `20260718_0003` performs its planning-text and private EntityLink JSON
encryption online because the deployment key must never be embedded in offline
SQL. Both edge metadata and provenance are covered. It refuses
`alembic ... --sql` instead of emitting a stampable partial revision. Its
downgrade likewise preflights decryption before removing V3 tables and refuses
to proceed with a missing, rotated, or incorrect key.

Revision `20260719_0004` moves contacts and CardDAV configuration into the
encrypted SQL authority. It enforces direct owner foreign keys, a composite
source-owner foreign key, encrypted connector UIDs with keyed lookup digests,
and source-scoped UID uniqueness so contacts cannot cross account boundaries.
CardDAV writes commit first to an encrypted, replay-safe delivery outbox and
use remote ETag preconditions. Runtime startup adopts legacy JSON only after
creating byte-identical private backups and resolving one unambiguous owner.
With multiple admins, `RESTIA_CONTACTS_IMPORT_OWNER` is required. Downgrade
refuses to discard any contact authority rows because legacy backups cannot
restore changes made after cutover.

Revision `20260720_0005` moves calendars and events to immutable Account.id
ownership while retaining the legacy username alias and the existing CalDAV
connector UUID. It adds optimistic event/configuration versions, encrypted
server-owned reversal snapshots, and an encrypted claim/lease/FIFO delivery
outbox. Event identity is the composite `(uid, owner_id)`, so separate owners
can store the same RFC UID; planning links use `(uid, calendar_id)` instead of
reintroducing a global UID constraint. Legacy calendar owners are backfilled
only when a normalized username
matches exactly one Account, or an ownerless calendar has exactly one active
Account; every other mapping fails before DDL. The revision is online-only,
uses owner-matching composite foreign keys, and refuses downgrade when action
authority, connector configuration versions, event versions, or retained
contact authority could be lost.

Revision `20260721_0006` makes Telegram link codes, encrypted chat principals,
and encrypted conversation bindings canonical SQL state keyed by immutable
`Account.id`. Exact chat lookup uses a bot-scoped keyed digest; link codes are
short-lived, one-time digests consumed transactionally. The explicit legacy
import validates complete ownership before committing, records only encrypted
metadata, and leaves `settings.json` unchanged as a recovery source. Runtime
readers have no JSON fallback. Downgrade refuses to discard any retained
Telegram principal, binding, link code, or import marker.

Revision `20260722_0007` adds Account.id-owned, approval-backed outbound email
draft and delivery authority. Agent-generated sends are persisted before review
and are delivered only through the durable outbox after the corresponding
ActionProposal is approved.

Revision `20260723_0008` widens the encrypted LifeEntity discriminator for the
typed `finance_record` authority. It adds no bank connection or executor state;
finance remains record/read analysis only, and any future real-world financial
mutation stays outside this schema as a separately reviewed Level 6 action.
Downgrade refuses to narrow the constraint while Finance records exist.

Revision `20260724_0009` makes Telegram polling and delivery coordination
canonical SQL state. One database-fenced lease owns each bot cursor; poison
attempts resolve into safe dead-letter metadata, and encrypted inbound/reply
rows use separate expiring claim digests. Legacy JSON and SQLite sidecars are
bounded, exact-bot import sources only. Downgrade refuses to discard retained
polling, inbound, dead-letter, or import state.

Revision `20260725_0010` moves reminder delivery claims, a shared
cancel-before-enqueue barrier, and the browser notification outbox into the
configured database. Every row is owned by immutable `Account.id`; payloads
and linked claim tokens are encrypted, while token/dedupe comparisons use
keyed digests. Startup reads retained V2 sidecars only through a bounded,
idempotent import with encrypted markers and never deletes the recovery source.
Downgrade refuses to discard any retained notification or import state.

Revision `20260726_0011` moves the email-to-Life projection handoff from its
SQLite sidecar into the configured database. Rows are owned by immutable
`Account.id`; subjects and RFC thread identifiers remain only in encrypted
JSON, while digest-only claim tokens and optimistic versions fence expiring
leases. The legacy ledger and message index are read through a bounded,
idempotent importer with encrypted cursor markers. It never deletes the
sidecar. New cache writes include an encrypted immutable recovery envelope so
the cross-database crash window does not discard RFC thread evidence, while
all claim/lease state remains canonical SQL. Downgrade refuses to discard
retained projection/import state.

Revision `20260727_0012` moves mutable email runtime state into the configured
database. Encrypted `Account.id`-owned rows now hold tag/spam state, automation
rules, manual schedules, and per-message automation results. Search identity
and claim tokens are keyed digests; database-time leases plus optimistic
versions fence LLM, calendar, IMAP, and SMTP side effects. Retained
`email_tags`, `scheduled_emails`, calendar extraction, and event-seen tables
are bounded query-only import sources and are never modified. Local email
SQLite now contains only rebuildable connector and LLM caches. Downgrade
refuses to discard retained email runtime or import rows.

Revision `20260728_0013` moves cross-interface profile settings, UI
preferences, feature choices, and user-created integration configuration into
encrypted, `Account.id`-owned SQL rows with optimistic versions, tombstones,
and keyed idempotency digests. Retained JSON files are bounded, read-only
import sources rather than runtime fallback writers.

Revision `20260729_0014` moves chat-upload metadata from `uploads.json` into
encrypted, `Account.id`-owned SQL rows. Owner/content uniqueness uses a keyed
digest, retention is versioned and tombstoned, and filesystem paths are opaque,
confined blob keys. The bounded legacy importer preserves its source and
records encrypted per-owner checkpoints. Downgrade refuses retained upload or
import rows because post-cutover metadata cannot be reconstructed from the
legacy source.

Revision `20260730_0015` adds database-time singleton-worker leases with
monotonic fencing tokens. Task scheduling, Telegram/email polling, upload
cleanup, notification delivery, call alerts, and maintenance workers can run
on every shared replica while exactly one current lease holder performs each
role; a paused or expired holder cannot renew or release a newer lease.

Normal Restia startup is the supported empty-database path because it records
the immutable 0001 boundary before executing the reviewed chain. Direct/offline Alembic runs
must use `alembic upgrade 20260716_0001:head`. Shared/PostgreSQL mode requires
PostgreSQL 14+, psycopg 3, authentication with localhost bypass disabled, one
shared encryption key, and a shared-filesystem blob root. The runtime gate is
enabled only while the reviewed machine-readable blocker list remains empty;
`scripts/odysseus-db shared-gate --migration-smoke --pretty` verifies the live
database and an isolated temporary-schema migration before deployment.

Useful diagnostics:

```bash
scripts/odysseus-db status --pretty
scripts/odysseus-db shared-gate --migration-smoke --pretty
scripts/odysseus-db stamp-legacy --pretty
alembic current
alembic heads
```
