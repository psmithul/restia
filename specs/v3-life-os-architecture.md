# Restia V3 Life OS Architecture

Status: accepted implementation direction; delivery is tracked in
`specs/v3-life-os-requirements.md`.

## Product boundary

Restia V3 is a consolidation of the existing assistant into one personal life
control plane. It is not a second application and it is not a collection of
new dashboards. Existing chat, projects, planning, calendar, contacts, notes,
documents, email, memory, search, automations, Study Mode, Home Link, calling,
and provider integrations remain the capability base.

The primary contract is:

> Ask Restia what to do, know why it matters, and let it handle approved
> execution.

The five primary destinations are Restia, Today, Inbox, Life, and Search.
Specialist tools remain reachable contextually from Life, search, commands, and
deep links instead of occupying permanent top-level navigation.

## Release contract

This specification is a major product update. Its first complete release is
published as `v3`. Restia keeps an internal three-component version for
packaging compatibility, so the corresponding `APP_VERSION` is `3.0.0`.

Public release precision communicates scope:

| Scope | Internal version | Public tag and image tag |
|---|---|---|
| Major | `X.0.0` | `vX` and `X` |
| Slightly major | `X.Y.0` | `vX.Y` and `X.Y` |
| Minor | `X.Y.Z` | `vX.Y.Z` and `X.Y.Z` |

Historical releases keep their published identifiers. New releases follow this
contract and are validated before container builds begin.

## One API, one identity, one data plane

Every interface talks to the canonical Restia API. Web, desktop, mobile,
Telegram, voice, CLI, automations, and linked-instance adapters must not each
create their own user database or bypass Restia's authorization and audit
layer.

The identity key is an immutable principal UUID. Usernames, email addresses,
display names, Telegram links, API tokens, and external OIDC subjects are
aliases or credentials attached to that principal. Mutable usernames must no
longer be the long-term ownership key for new V3 records.

The migration is additive:

1. Create a principal for every existing local profile.
2. Map the legacy normalized username to the principal.
3. Issue database-backed web/native sessions for the principal.
4. Add `principal_id` to new V3 records immediately.
5. Backfill existing owner-scoped tables behind compatibility resolvers.
6. Retire username-owned writes only after every supported store is migrated
   and verified.

Authentication failures remain fail-closed throughout the migration. Legacy
credential files are imported idempotently and preserved for rollback until a
verified backup exists; they do not remain a second writable source of truth.

## Database deployment decision

SQLAlchemy remains the application data boundary.

- SQLite is the default for a private, single-node, offline-capable install.
- PostgreSQL is the supported shared deployment for multiple devices or
  multiple Restia API replicas.
- Supabase may provide managed PostgreSQL and standards-based OIDC/JWT identity,
  but it is an optional deployment adapter, not a required vendor dependency.
- Browser/mobile clients never query Supabase tables directly. Direct client
  access would bypass Restia's domain permissions, confirmations, audit trail,
  and reversible-action rules.
- ChromaDB and other vector indexes are derived retrieval indexes, not the
  system of record.
- Files remain in the configured durable file store; SQL rows hold ownership,
  provenance, hashes, versions, and storage references. An object-store adapter
  can be added for shared deployments without changing domain APIs.

PostgreSQL support is incomplete until the runtime includes a tested driver,
schema migrations run on both SQLite and PostgreSQL, and no migration helper
assumes a raw SQLite connection. A `DATABASE_URL` setting alone is not proof of
PostgreSQL support.

## Unified life model

V3 adds a cross-domain graph without replacing proven domain tables.

Core entity types are Person, Area, Goal, Project, Milestone, Task, Event,
Message, Note, File, Decision, Habit, Metric, Transaction, Health Record,
Place, Asset, Reminder, Automation, and Source.

Each graph record has:

- immutable ID and `principal_id`;
- type, title, status, timestamps, and optimistic version;
- provenance and confidence;
- an optional typed reference to an existing domain row;
- bounded metadata for properties that are not yet promoted to typed columns.

Edges are owner-scoped, typed, auditable relationships between entities. They
never grant access across principals. Domain tables remain authoritative for
domain behavior; the graph provides traversal, context, search, reasoning, and
cross-domain relationships.

## Universal Inbox

Every capture enters one owner-scoped ingestion contract before it becomes a
task, event, note, person update, project fact, decision, reference, expense,
goal, habit, someday idea, or archive item.

An inbox item records its source, raw-content reference, safe preview,
classification, confidence, processing state, linked entity, and audit events.
Classification may be deterministic or model-assisted through the single
`LLMProvider` interface. Low-confidence classifications stay reviewable and no
external action is executed merely because an item was classified.

Existing email, chat, upload, note, Telegram, calendar, and file flows are
integrated through adapters; they are not rewritten into parallel feature
implementations.

## Planning and execution

Today is an action-oriented aggregation over the unified model and existing
domain services. Recommendations expose what to do, why now, estimated effort,
delay cost, supported goal/project, source evidence, and what Restia can handle.

Focus Mode leases one active item, opens linked context, starts a recoverable
timer, captures interruptions, records evidence, and creates follow-ups. It
extends the existing persisted planning and Study timer patterns rather than
introducing a browser-only timer.

## Permissions, actions, and audit

Every proposed action carries a domain and autonomy level:

1. Observe
2. Suggest
3. Prepare
4. Execute reversible
5. Execute external after approval
6. High risk; confirmation is always required

Domain policies cap the level Restia may use. Sending personal communications,
bookings, financial actions, legal/medical actions, permission changes, and
destructive operations cannot be authorized by model output alone.

Every mutation records actor, interface, sources, reason, target, outcome,
reversibility, and an undo/version reference when applicable. The audit log is
append-only through ordinary application APIs.

WhatsApp remains read-only until the user explicitly approves a separately
designed reply workflow. No generic send capability is added to its client
wrapper.

## Offline and synchronization model

The server database is authoritative for a Restia installation. Offline clients
use a local cache plus an operation outbox; they do not run an uncoordinated
multi-master copy of the full database. Mutations carry entity versions and
idempotency keys. Conflicts are surfaced explicitly instead of silently using
last-writer-wins for decisions, money, health, permissions, or external
communications.

Home Link remains an instance-to-instance transport. It does not become a
backdoor around principal ownership or domain permissions.

## Delivery order

1. Contract tests and a requirement ledger.
2. Stable principal identity, shared sessions, PostgreSQL readiness, and legacy
   migration.
3. Universal Inbox, sources, life entities/edges, permissions, and audit core.
4. Restia/Today/Inbox/Life/Search shell and conversational control-plane tools.
5. Goals/projects/tasks/calendar/people/notes/files/search/focus/review
   consolidation.
6. Connected Life adapters.
7. Proactive intelligence and bounded delegation.
8. Ambient/mobile/offline capabilities.
9. Requirement-by-requirement verification, backup/migration rehearsal, and
   the `v3` shipped-install release gate.

No phase is complete because a dashboard exists. Completion requires persisted,
owner-scoped behavior exercised through the canonical API and verified in the
actual interface or shipped runtime.
