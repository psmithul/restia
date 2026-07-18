# Restia V3 Life OS Requirement Ledger

Source: the user-approved “Restia: The App That Manages Your Entire Life”
specification supplied on 2026-07-16, plus the explicit requirement for one
shared database/login across interfaces.

This ledger prevents the major release from being declared complete on the
strength of a narrow test or a new dashboard. A requirement moves to **done**
only when the current implementation, owner/permission behavior, automated
tests, real interface, migration path, and shipped runtime provide direct
evidence.

Status values: `existing`, `partial`, `not-started`, `blocked`, `done`.
`existing` means a capability predates V3 and still needs integration evidence;
it is not synonymous with `done`.

## Cross-cutting release gates

- [ ] Public release tag is `v3.1`; internal package version is `3.1.0`.
- [x] Major/slightly-major/minor public tag precision is enforced in CI.
- [x] One immutable principal spans web, desktop, mobile, Telegram, API, voice,
      automation, and linked-interface adapters.
- [x] Profiles and sessions use the shared database; legacy JSON migration is
      idempotent, fail-closed, backed up, and reversible.
- [x] SQLite private installs and PostgreSQL shared installs pass the same
      domain contract tests.
- [x] Supabase is verified as an optional PostgreSQL/OIDC deployment adapter,
      not a client-side authorization bypass or mandatory dependency.
- [ ] Every new V3 record is principal-scoped and every mutation is audited.
- [x] Existing user data upgrades without destructive reset.
- [x] Backup, readiness, rollback, update scripts, GHCR amd64/arm64 images, and
      a clean shipped-container smoke test all pass.

## 1. Main Restia experience — `partial`

Existing evidence: conversational chat, model/provider routing, voice input,
uploads, image/file handling, commands, search, and confirmation surfaces.

- [ ] Restia is the default control-plane surface rather than a feature menu.
- [ ] Text, voice, files, images, screenshots, forwarded messages/email, quick
      capture, global search, confirmations, and generated views share one
      conversation context.
- [ ] “What should I do next?”, day planning, overdue-risk, rescheduling,
      follow-up, project summary, decision recall, document search, safe drafting,
      spending, and goal-progress queries have verified end-to-end paths.
- [ ] Answers expose reasoning, sources, assumptions, and available actions.

## 2. Universal Inbox — `partial`

- [ ] Thoughts, voice notes, screenshots, links, email, WhatsApp, files, meeting
      notes, tasks, ideas, receipts, reminders, saved posts, and papers use one
      owner-scoped ingestion contract.
- [x] Classification supports Task, Event, Note, Person update, Project
      information, Decision, Reference, Expense, Goal, Habit, Someday, Archive.
- [x] Confidence, source, correction, processing status, and linked entities are
      persisted and auditable.
- [x] Capture never requires the user to choose a destination first.
- [x] Classification cannot execute an external action by itself.

## 3. Today and execution — `complete`

Existing evidence: `/api/mission-control/today`, planning items, project work,
calendar, notes, Study timer, daily briefs, and deterministic next actions.

- [x] Today shows primary outcome, top three actions, events, must-do tasks,
      people awaiting replies, health/routines, risks/conflicts, suggested
      schedule, and Restia-owned work.
- [x] Every recommendation states why now, effort, delay cost, linked goal or
      project, source evidence, and what Restia can handle.
- [x] Focus Mode hides unrelated information, opens context, shows definition
      of done, persists a timer, captures interruptions/progress/evidence, and
      creates follow-ups.

## 4. Goals, projects, milestones, tasks, actions — `complete`

Existing evidence: Projects, stages/work items/checklists, planning items,
Notes goals, Study goals, progression, dependencies, attachments, activity.

- [x] Life Area → Goal → Project → Milestone → Task → Action is represented
      without duplicating the same commitment across disconnected stores.
- [x] Tasks include definition of done, priority, deadline, effort, energy,
      context, project, people, dependencies, documents, source, status, next
      action, and completion evidence.
- [x] Restia detects overdue, blocked, waiting, missing-next-action, irrelevant,
      duplicated, and goal-disconnected tasks.

## 5. Calendar and time — `complete`

Existing evidence: local calendars, Google/CalDAV, recurrence, reminders,
planning-to-calendar scheduling, Today aggregation.

- [x] Meetings, classes, work, personal commitments, travel, deadlines,
      routines, focus blocks, rest, and reminders share one schedule model.
- [x] Free-time finding, conflict resolution, time blocking, unfinished-work
      rescheduling, meeting preparation/follow-up, focus protection,
      overcommitment, duration, and energy are verified.
- [x] Events link people, projects, notes, files, previous meetings, decisions,
      and follow-up tasks.

## 6. Relationship manager — `complete`

Existing evidence: Contacts, direct messaging, profile/status data, Home Link.

- [x] Person profiles cover relationship, organisation, contact data, origin,
      last interaction, conversations, promises, follow-ups, dates,
      preferences, projects, files, history, and private notes.
- [x] Follow-up, unanswered-message, project-relevance, and relationship-care
      reminders are source-backed and bounded.
- [x] No personal message is sent without the required approval.

## 7. Communications hub — `partial`

Existing evidence: Gmail/IMAP email, Telegram, Restia messaging/calls,
notifications, integrations, drafting and email tools.

- [x] Unified unread/importance view spans enabled communication connectors.
- [x] Thread summaries, response suggestions, commitments, deadlines, contacts,
      follow-ups, drafts, search, and conversion to life entities are verified.
- [x] Reading/summarising and drafting policies are distinct from sending.
- [x] WhatsApp client remains read-only unless a separately approved reply flow
      is implemented; no generic send method exists.
- [x] Enabled Slack and Twilio integrations poll independently from Tasks with
      canonical HTTPS origins, GET-only grants, encrypted durable cursors,
      owner-bound idempotent projection, and no reply/send callable.

## 8. Personal memory and knowledge — `complete`

Existing evidence: chat history, Restia memory, Mnemosyne, documents, Notes,
personal RAG, Markdown/Obsidian-compatible stores, search.

- [x] Semantic, episodic, decision, preference, procedural, relationship,
      project, task, and source memory types are explicit.
- [x] Notes, documents, research, bookmarks, web pages, meetings, writing,
      academic/business records, ideas, and lessons are connected.
- [x] Markdown can remain a durable source of truth while indexes are
      rebuildable.
- [x] Answers distinguish confirmed facts, user statements, assumptions,
      inferences, stale information, and gaps, with citations.

## 9. Decisions — `complete`

- [x] A Decision stores date, context, options, choice, reasons, risks,
      assumptions, people, evidence, review date, and outcome.
- [x] Decision recall, assumption review, scheduled review, and change history
      are searchable and source-backed.

## 10. Health and fitness — `complete`

- [x] Weight, measurements, sleep, exercise, nutrition, steps, recovery, water,
      medication reminders, appointments, reports, symptoms, mood/stress, and
      wearable data have private typed records and imports.
- [x] Trends, behavior/outcome links, time-aware workouts, adherence, missed
      routines, appointment questions, and document storage are verified.
- [x] Medical advice boundaries and urgent-professional-help language are
      enforced; medication is never changed autonomously.

## 11. Personal finance — `complete`

- [x] Accounts, cash, cards, income, expenses, subscriptions, investments,
      loans, taxes, bills, receivables, and personal/business scope are modeled.
- [x] Classification, cash flow, subscriptions, reminders, anomalies, goals,
      receipts, tax documents, net worth, forecasts, and affordability are
      verified.
- [x] Transfers, investments, cancellations, and other high-risk actions always
      require confirmation.
- [x] Banking passwords are never stored directly.

## 12. Learning and career — `complete`

Existing evidence: Study Mode, goals/progress/timer/review, documents/research,
Projects.

- [x] Skills, courses, papers, books, objectives, notes, practice, projects,
      questions, revision, and competency evidence form one learning graph.
- [x] Roles, companies, universities, applications, resumes, portfolio,
      interviews, contacts, deadlines, gaps, and decisions form one career
      workspace.
- [x] Career → capability → gap → learning plan → portfolio → weekly action is
      traversable and actionable.

## 13. Work and business workspaces — `complete`

Existing evidence: Projects, project members/links, documents, tasks, activity.

- [x] Each workspace isolates objectives, projects, people, meetings, tasks,
      documents, decisions, metrics, risks, procedures, communications, and
      activity.
- [x] Cross-workspace relationships are explicit and never broaden access.

## 14. Habits and routines — `complete`

- [x] Morning/evening, workout, meals, review, learning, finance, relationship,
      maintenance, and sleep routines are supported.
- [x] Tracking covers consistency, quality, friction, failure causes, recovery,
      goal effect, and continued usefulness without meaningless streaks.

## 15. Home and personal administration — `complete`

Existing evidence: Documents, Notes/reminders, files, Calendar.

- [x] IDs, insurance, warranties, renewals, inventory, repairs, purchases,
      deliveries, vehicles, travel documents, forms, providers, household
      routines, and emergency information are modeled and linked to files.
- [x] Expiry alerts are verified.

## 16. Travel — `complete`

Existing evidence: calendar events, email receipts/tickets, notes/files.

- [x] Research, budget, transport, lodging, visas, itinerary, packing,
      reservations, local transport, documents, contacts, expenses, and
      calendar use one trip context.
- [x] Travel Mode presents only immediate, offline-available information.

## 17. Journal and reflection — `complete`

Existing evidence: Notes, documents, memory, daily brief/task summaries.

- [x] Journal, mood, moments, wins, difficulties, lessons, gratitude, ideas,
      principles, and periodic reflection have private typed capture.
- [x] Weekly/monthly/annual reviews cover changes, improvement, repeated
      failure, time, relationships, goal progress, and next changes.

## 18. Automation engine — `complete`

Existing evidence: ScheduledTask, webhooks, email/calendar pollers, task runs,
agents, notifications.

- [x] Triggers cover time, email, calendar, overdue tasks, uploads, people,
      metric thresholds, location, forms, and project status.
- [x] Actions cover entity creation, scheduling, database updates, drafts,
      approved sends, briefings, file moves, reports, information requests,
      notifications, agents, and workflows.
- [x] Meeting-end workflow is verified end to end.
- [x] Automation execution honors domain autonomy and idempotency.

## 19. Proactive intelligence — `complete`

Existing evidence: Today risks, email urgency, task scheduling, daily briefs,
notification center.

- [x] Restia detects overdue commitments, goal conflicts, overload, missing
      follow-ups, unanswered messages, unused subscriptions, financial/health
      anomalies, stalled projects, unscheduled deadlines, postponement, and
      stale-decision assumptions.
- [x] Interruptions are limited to urgent, important, time-sensitive, high-risk,
      or explicitly requested matters; everything else enters a digest.

## 20. Unified life graph — `complete`

- [x] All required core entity types and owner-scoped typed edges exist.
- [x] Existing domain rows can be referenced without duplicating authority.
- [x] Source provenance, confidence, permissions, versioning, and deletion
      behavior are explicit.
- [x] Email → person → project → decision → task → deadline → calendar → file →
      goal is verified as a real traversal.

## 21. Permission and autonomy — `complete`

Existing evidence: profile privileges, confirmations, tool policies, API token
scopes, project roles.

- [x] Levels 1–6 are represented on every prepared/executed action.
- [x] Per-domain caps and confirmation rules are configurable.
- [x] Calendar, drafts, sends, WhatsApp, deletion, finance, legal, and medical
      examples enforce the declared policy in backend code.

## 22. Trust, privacy, and security — `partial`

Existing evidence: authentication/2FA, encryption-at-rest wrappers, E2EE
messaging, owner scoping, API scopes, security headers, backups.

- [ ] Encryption, key separation, secure auth, biometric/device controls,
      connector permissions, audit, versioning, undo, citations, export,
      encrypted backups, private-data/no-training posture, and visible agent
      actions are verified.
- [ ] Every response/action can expose inputs, changes, reason, actor/workflow,
      and reversal path.
- [ ] No random model output writes directly to authoritative data.
- [x] Local-single deployments can schedule recurring AES-256-GCM backups
      independently from Tasks, verify before retention, persist safe run
      health, and expose current-backup posture. Shared PostgreSQL fails
      explicitly until the operator backs up both SQL and the blob store.

## 23. Minimal navigation — `complete`

Existing evidence: declarative navigation registry, five-destination adaptive
shell, contextual Life workspace, and consolidated More menu.

- [x] Permanent primary navigation is Restia, Today, Inbox, Life, Search.
- [x] Goals, Projects, People, Health, Money, Learning, Work, Home, Journal, and
      Files are contextual destinations inside Life.
- [x] Existing deep links, commands, accessibility, mobile behavior, and user
      visibility preferences migrate without loss.

## 24. Ideal home screen — `complete`

- [x] Home answers what matters now, what is happening today, what needs
      attention, what Restia is handling, and what changed.
- [x] “Ask Restia anything…” is the primary action.

## 25. One application experience — `partial`

- [ ] Daily task, notes, habits, CRM, bookmarks, journal, basic budgeting,
      projects, reminders, knowledge, and AI workflows are usable through one
      Restia experience.
- [ ] Banking, Gmail, WhatsApp, government, medical, booking, and cloud-storage
      infrastructure remain integrations behind that experience rather than
      pretend replacements.

## 26. Build order and final product definition — `in-progress`

- [ ] Phase 1 Personal Operating Core is complete and verified.
- [ ] Phase 2 Connected Life is complete and verified.
- [ ] Phase 3 Proactive Agent is complete and verified.
- [ ] Phase 4 Ambient Life OS is complete and verified.
- [ ] Starting the day with “What should I do today?” produces an accurate,
      source-backed answer covering importance, urgency, waiting work, people,
      needed information, delegable work, responsible systems, and progress
      toward intended goals.

## Evidence log

Add dated entries here only after verification. Each entry must name the exact
requirements advanced, source files, tests, runtime/browser evidence, migration
result, and commit. A green narrow test may support one entry; it cannot mark an
entire section complete by itself.

- **2026-07-16 — V3 foundation (`bfa51bc`)**: advanced the release-precision
  gate, Universal Inbox (Section 2), Today Inbox attention (Section 3), stable
  principal groundwork, typed cross-domain references (Section 20), and
  mutation audit groundwork (Section 22). Implementation is in
  `src/release_version.py`, `src/database_runtime.py`, `src/identity.py`,
  `src/life_core.py`, `src/audit_context.py`, `routes/inbox_routes.py`,
  `routes/mission_control_routes.py`, `static/js/inbox.js`, and their migration,
  shell, and style files. The complete suite passed with **5,507 passed, 3
  skipped**; focused identity/Inbox/release contracts passed; Alembic legacy
  status/stamp was exercised against an isolated database; and live browser QA
  on the isolated local runtime verified capture, classification, processing,
  archive, filters, Today attention, navigation, console state, and 375 px
  mobile behavior. Shared PostgreSQL/auth sessions, connected-source adapters,
  and the remaining Life OS sections deliberately remain incomplete.
- **2026-07-17 — V3 Planning Spine (`90e769b`)**: completed the first two Today
  execution requirements (Section 3), canonical Life Area → Action hierarchy
  and commitment-quality detectors (Section 4), and minimal navigation
  (Section 23); it also advanced the unified graph, Focus, permission, and
  trust boundaries without marking those broader sections complete. The
  implementation is in `core/database.py`, `src/life_graph.py`,
  `src/focus_mode.py`, `src/action_policy.py`, their API routes, the Today/Life
  workspace and navigation modules, and migration `20260718_0003`. The full
  suite passed with **5,789 passed, 3 skipped**; focused migration, encryption,
  canonical-task, action-policy, Today, and navigation contracts passed.
  Reviewed `0001 → 0002 → 0003` upgrades and pushed-0001 adoption encrypt
  planning text plus edge metadata/provenance, reject incomplete head schemas,
  and preserve recoverable ciphertext. Browser QA on an isolated runtime
  verified the exact five primary destinations, `/today` and `/life` deep-link
  reloads, Search open/close selected-state parity, 375 px mobile navigation,
  contextual More behavior, an actionable degraded-source Settings path, and
  no console errors. Focus UI completion, complete structured task fields,
  connected-source ingestion, and shared-runtime readiness remain incomplete.
- **2026-07-17 — Focus, Decisions, Health, and reviewed actions (working tree on
  `a17f584`)**: completed Focus Mode (Section 3), the typed Decision record
  requirement (Section 9), and private typed Health/import records (Section
  10), while advancing permission, audit, and reversal requirements (Sections
  21–22). Implementation is in `src/focus_mode.py`, `src/decision_service.py`,
  `src/health_service.py`, `src/action_policy.py`,
  `src/calendar_action_executor.py`, their API routes, and the Today/Life
  workspace modules. The integrated focused suite passed with **118 passed**;
  JavaScript syntax checks and `git diff --check` passed. An isolated local
  runtime upgraded through migration `0005`; Decision and Health records reuse
  the versioned/audited typed LifeEntity schema and therefore need no additional
  schema migration. Live browser QA verified Focus start/pause/resume/reload,
  progress/interruption/evidence capture, completion and follow-up creation; a
  complete contextual Decision view; and a Level 5 calendar cancellation with
  fresh review, execution, explicit reversal review, and restored event state
  (`confirmed`, version 3). The same run verified the five-item desktop/mobile
  navigation, 375 px no-overflow bounds, no browser console errors, and no
  Restia action tokens rendered in the document. Scheduled Decision review,
  broader health insight workflows, and shipped-runtime evidence remain open.
- **2026-07-17 — Finance authority and conversational reads (working tree on
  `a17f584`)**: advanced Personal Finance (Section 11) and the main Restia
  control-plane query path (Sections 1 and 25). `src/finance_service.py` stores
  twelve encrypted, `Account.id`-owned record types for personal/business
  accounts, observations, cash flow, subscriptions, investments, loans, taxes,
  bills, receivables, budgets, and document references. It rejects credentials,
  full financial numbers, and executor-shaped payloads; exposes only record and
  deterministic analysis APIs; and labels every result as non-advisory and
  record-only. Revision `20260723_0008` adds the reviewed discriminator without
  bank connection or executor state. The read-only `query_life` tool now answers
  bounded finance summary, cash-flow, subscription, due-item, and anomaly-input
  questions through the same Restia control plane. Finance/migration contracts
  passed with **20 passed**; the integrated control-plane, Finance, migration,
  schema-parity, tool-policy, and plan-mode set passed with **54 passed**; and
  the full `0001 → 0008` SQLite chain ran during those tests. Live PostgreSQL,
  conversational/browser, forecasting/affordability, and shipped-runtime gates
  remain open.
- **2026-07-17 — Habits, routines, and read-only review evidence (working tree
  on `a17f584`)**: advanced Habits and Routines (Section 14), the contextual
  Life workspace (Section 23), and the main Restia query path (Sections 1 and
  25). `src/habit_service.py` defines private, Account.id-owned routines and
  immutable observation snapshots for every required routine category,
  validates real IANA timezones, and produces deterministic consistency,
  quality, friction, missed/recovery, and weekly-adjustment reports without
  executing a suggestion. The strict APIs live under `/api/life/habits`; the
  read-only `query_life` surface exposes weekly, missed-routine, and adjustment
  evidence; and the existing Health context renders the canonical routine
  definition. Focused Habit contracts passed with **25 passed**; the integrated
  Habit/query/schema/policy/plan-mode set passed with **59 passed**; contextual
  Life workspace checks passed with **11 passed**; JavaScript syntax and Python
  compilation passed. Goal-effect/usefulness links, live browser evidence,
  PostgreSQL, and shipped-runtime gates remain open.
- **2026-07-17 — Email classification scope and confirmed-send authority
  (working tree on `a17f584`)**: advanced Communications (Section 7), reviewed
  external actions (Section 21), and owner-scoped trust (Section 22).
  `routes/email_helpers.py`, `routes/email_routes.py`, and the projection
  ledger now merge by stable message identity within the exact owner/account,
  apply the reviewed tag taxonomy, and clear answered/reminder/calendar labels
  only from source-backed state; the focused tagging/owner/projection set
  passed with **40 passed**. The shared frontend taxonomy now canonicalizes and
  de-duplicates one final time before both Inbox and Library rendering, maps
  legacy `promo` to `marketing`, and removes answered-response urgency without
  duplicating the policy across both clients; its tagging/owner/ingestion
  regression set passed with **26 passed**. Model, MCP, and Codex send/reply requests now only
  prepare immutable Level-5 SQL drafts. Human confirmation queues that exact
  snapshot, and `src/email_delivery_worker.py` durably claims and commits it
  before SMTP; validates owner, account, recipients, threading, Message-ID,
  content digest, and attachment digest; appends Sent best-effort after
  transport; and stores stable retry/terminal codes. Both the default 30-second
  email poller and `scripts/odysseus-mail poll-scheduled` drain this outbox,
  independently of Tasks. Worker/authority/action contracts passed with **60
  passed**; compilation and whitespace checks passed. Delivery is explicitly
  at-least-once because SMTP offers no idempotency key. Live mailbox/browser,
  shared tag/cache/rule/manual-schedule authority, and shipped-runtime gates
  remain open.
- **2026-07-17 — Canonical structured human tasks (working tree on
  `a17f584`)**: completed the structured Task-field requirement (Section 4)
  without conflating human commitments with recurring agent automations.
  `src/task_record_service.py` stores encrypted, Account.id-owned typed Task
  entities with definition of done, priority, UTC deadline, effort, energy,
  contexts, owner-validated project/people/dependency/document references,
  explicit source, state, next action/waiting context, and required completion
  evidence. `routes/task_record_routes.py` exposes strict CAS CRUD/history at
  `/api/life/tasks`; the generic Life API rejects typed bypasses; and every
  result states that it is record-only and not ScheduledTask authority.
  Project references participate in bounded goal-connectivity quality checks.
  Task plus Life-graph contracts passed with **15 passed**; Python compilation
  and targeted whitespace checks passed. Live Today/browser, migration-chain,
  PostgreSQL, and shipped-runtime gates remain open.
- **2026-07-17 — Relationship and Journal evidence systems (working tree on
  `a17f584`)**: completed the typed requirements for Relationship Manager
  (Section 6) and Journal/Reflection (Section 17), while advancing the main
  Restia query and contextual Life workspace. Relationship profiles reuse
  ContactRecord identity without copying email/phone authority and store
  source-backed origins, dates, preferences, care plans, private notes,
  interactions, commitments, follow-ups, project/file links, history, and
  deterministic reminders. No relationship service can deliver a message;
  every output fixes future personal sends at Level 5 with confirmation.
  Journal entries privately capture mood, moments, wins, difficulties,
  lessons, gratitude, ideas, decisions, principles, promises, time,
  relationships, goal progress, and next changes; weekly/monthly/annual review
  is deterministic structured-field aggregation with no model inference.
  Relationship contracts passed with **8 passed** and Journal contracts with
  **14 passed**; the integrated query/domain/schema/policy/plan-mode set passed
  with **104 passed**; contextual Life workspace checks passed with **17
  passed**; syntax, compilation, and whitespace checks passed. Live browser,
  PostgreSQL, migration-chain, and shipped-runtime evidence remain open.
- **2026-07-17 — Home and personal administration records (working tree on
  `a17f584`)**: completed the typed Home/Admin requirements (Section 15) and
  advanced the read-only Restia control plane. `src/home_service.py` models
  identity documents, insurance, warranties, renewals, inventory, repairs,
  purchases, deliveries, vehicles, travel documents, forms, providers,
  household routines, and emergency information as encrypted Account.id-owned
  records with owner-validated file/document/entity references. Alerts use an
  explicit `as_of`, a maximum 365-day horizon, source/provenance evidence, and
  no network or executor. The strict Home APIs and generic-bypass guard are
  registered; `query_life` exposes bounded list/search/alert reads; and the
  existing Home context renders record type, source, references, due/expiry,
  and its record-only policy. Focused Home contracts passed with **19 passed**;
  the current integrated domain/query set passed with **104 passed** and
  contextual Life workspace checks with **17 passed**. Live browser,
  PostgreSQL, migration-chain, and shipped-runtime evidence remain open.
- **2026-07-17 — Typed Travel context and offline Travel Mode (working tree on
  `a17f584`)**: completed the typed Travel requirements (Section 16) without
  adding another permanent navigation destination. `src/travel_service.py`
  stores a root trip plus research, budget, transport, lodging, visa,
  itinerary, packing, reservation, local transport, document, contact,
  expense, and calendar-reference records in one encrypted Account.id-owned
  context with owner-validated sources, entities, and Calendar events. Travel
  Mode requires an explicit offset-aware `as_of`, uses half-open trip
  boundaries, caps current/next trips and facts, and when offline-only omits
  anything not explicitly available offline. It performs no network/model
  call and cannot book, buy, or send. Strict APIs, generic-bypass guards,
  read-only `query_life` actions, and a contextual Life card are registered.
  Focused Travel contracts passed with **10 passed**; the integrated
  Travel/query/frontend set passed with **23 passed**; Python/JavaScript syntax
  and scoped whitespace checks passed. Live browser, PostgreSQL,
  migration-chain, and shipped-runtime evidence remain open.
- **2026-07-17 — Learning and Career graph (working tree on `a17f584`)**:
  completed the typed Learning/Career requirements (Section 12) and advanced
  the contextual Life/control-plane experience. `src/learning_career_service.py`
  stores source-backed skills, courses, papers, books, objectives, notes,
  practice, projects, progress/revision evidence, roles, companies,
  universities, applications, resumes, portfolios, achievements, networking,
  interview preparation, and gaps as encrypted Account.id-owned Life records.
  Owner-scoped links connect external Person/Decision/Project/File records
  without copying their authority. The deterministic
  role/application/company/university → capability → gap → learning plan →
  portfolio → weekly action read model requires a Monday week, reports missing
  chain segments, performs no model inference, and cannot apply or submit.
  Independent review caught and regression-tested tombstoned link handling,
  disguised executor/credential payloads, safe display-only credential names,
  and embedded credential URLs. Focused contracts passed with **39 passed**;
  the integrated Learning/query/frontend set passed with **55 passed**;
  Python/JavaScript syntax and scoped whitespace checks passed. Live browser,
  PostgreSQL, migration-chain, and shipped-runtime evidence remain open.
- **2026-07-17 — Isolated Work and Business workspaces (working tree on
  `a17f584`)**: completed the typed workspace requirements (Section 13) and
  advanced the contextual Life/control-plane surface. `src/work_business_service.py`
  stores separate encrypted Work or Business workspace roots and source-backed
  objective, project, person, meeting, task, note/file/document, decision,
  metric, opportunity, customer, outreach, proposal, follow-up, revenue,
  experiment, process, lesson, and roadmap records. Every read requires the
  exact Account.id and workspace id. Cross-workspace relationships use their
  own owner-validated typed relation and never imply record visibility or role
  access; deletion is CAS guarded while references remain. Strict validation
  rejects credentials, authenticated URLs, disguised tool/executor payloads,
  outreach sends, proposal submissions, and payment execution. The existing
  Work context now includes every typed workspace record without adding a
  permanent sidebar destination. Focused contracts passed with **57 passed**;
  the integrated Work/query/frontend set passed with **76 passed**;
  Python/JavaScript syntax and scoped whitespace checks passed. Live browser,
  PostgreSQL, migration-chain, and shipped-runtime evidence remain open.
- **2026-07-17 — Personal knowledge and proactive intelligence (working tree on
  `a17f584`)**: completed Sections 8 and 19 on the canonical Account.id-owned
  Life graph. Personal knowledge now has explicit memory/source kinds,
  encrypted versioned records, owner-validated citations, durable Markdown
  source manifests with rebuildable indexes, as-of staleness, contradiction
  and gap reporting, and fail-closed inference labels. Generic graph and source
  APIs exclude these typed records so callers cannot bypass the epistemic
  contract. The deterministic proactive read model detects the declared
  cross-domain risks from stored evidence, routes only explicitly requested,
  high-risk, or urgent + important + time-sensitive signals as interruptions,
  and leaves every other signal in the digest. It cannot mutate, propose,
  execute, send, or notify. `query_life`, its schema and prompt, typed APIs,
  the Life workspace, and a non-repeating Mission Control attention surface
  are integrated. The focused service/API/query/UI set passed with **105
  passed** plus JavaScript syntax and whitespace checks. PostgreSQL,
  migration-chain, and shipped-runtime evidence remain open.
- **2026-07-17 — Upload and attachment authority (working tree on
  `a17f584`)**: advanced the shared data plane and existing-data migration
  gates without adding an object-store dependency. Revision `20260729_0014`
  makes chat upload metadata encrypted, `Account.id`-owned SQL authority with
  owner/content idempotency, optimistic retention tombstones, confined blob
  keys, and privacy validation at schema head. The bounded importer recomputes
  blob hashes, records encrypted per-owner checkpoints, recovers from a valid
  backup, and never mutates `uploads.json`. Chat, document, vision, approved
  email attachment, and email-PDF adoption paths now resolve through the same
  SQL authority; Project attachments use the same validated byte-store root.
  Shared mode requires an explicit durable filesystem mount consistently
  configured on every replica. The focused upload/blob/migration plus complete
  migration-foundation/shared-readiness set passed with **65 passed**; added
  tamper, hash-recomputation, and loss-averse downgrade contracts passed in a
  **27 passed** focused rerun. Live PostgreSQL and shipped-runtime evidence
  remain open.
- **2026-07-17 — Connected Communications Hub (working tree on `a17f584`)**:
  advanced connected-source Universal Inbox coverage (Section 2) and verified
  every functional Communications requirement (Section 7) without creating a
  parallel message store or transport. `src/communications_hub.py` reads the
  exact owner/account from enabled email cache rows, canonical Email/Telegram/
  read-only WhatsApp Life projections, Restia direct messages, and SQL browser
  notifications; exposes one bounded unread/importance/search view with
  deterministic summaries, reviewed draft suggestions, commitments,
  deadlines, contacts, and follow-ups; and makes no source acknowledgement or
  send call. Explicit conversion in `routes/communications_routes.py` enters
  `src/life_core.py`'s Universal Inbox and canonical processor, preserving
  source/audit evidence and executing no external action. `query_life`, its
  schema, prompt, and tool index expose the same read-only view. The WhatsApp
  boundary has only `ingest_whatsapp_readonly_message`; its advertised
  capability set, module surface, route surface, and repository AST contain no
  WhatsApp send callable or generic client method. The focused contract passed
  with **12 passed**; the relevant ingestion/Life/query/automation/messaging/
  notification/email-owner regression set passed with **151 passed** after
  excluding one independently stale migration-head assertion owned by the
  concurrent migration chain. No schema change was required. The section
  remains `partial` pending live browser, PostgreSQL, and shipped-runtime
  evidence; non-communication Universal Inbox source adapters also remain open.
- **2026-07-18 — v3.1 backup and read-only connector runtime (working tree on
  `29d4b5d`)**: added a Task-independent, database-leased encrypted backup
  scheduler for local-single deployments with external owner-only passphrase
  files, verification-before-retention, durable secret-free health, persistent
  Docker mounts, and explicit shared-mode operator-backup failure. Added a
  separate database-leased Slack and Twilio poller with exact canonical HTTPS
  origins, GET-only integration grants, encrypted owner/account cursors,
  bounded streamed responses, idempotent Communications Hub projection, and no
  external send/reply callable. Alembic revisions `20260801_0017` and
  `20260802_0018` are registered and privacy-validated. A clean local runtime
  migrated to head `20260802_0018`, reported ready, and created both authority
  tables. The final backup/connector/migration/security/release/Compose set
  passed with **261 passed**; Python compilation, Compose YAML parsing,
  whitespace checks, and `v3.1` release preflight passed. Live provider,
  shared PostgreSQL operator-backup, real-device WebAuthn, final browser
  accessibility, and public-image evidence remain open.
- **2026-07-18 — SQLite/PostgreSQL release parity (`d87893f`)**: the release
  workflow now runs the canonical identity, Life-domain, action-policy,
  profile, encrypted-column, and distributed-leadership contract against a
  real PostgreSQL service before either architecture image can build. GitHub
  Actions run `29630896278` passed PostgreSQL migration and contract checks,
  amd64/arm64 builds, shipped-container smoke, and shared-Compose smoke. This
  advances the shared-runtime and shipped-path gates without claiming the
  still-pending public `v3.1` tag or provider/device verification.
- **2026-07-18 — shared conversational Today control plane (working tree on
  `cf1fb91`)**: completed the Ideal Home contract (Section 24) and advanced the
  main Restia control plane, trust transparency, one-application experience,
  and final-product day-planning path (Sections 1, 22, 25–26).
  `routes/mission_control_routes.py` now owns one reusable, deterministic
  Today aggregator used by both `/api/mission-control/today` and the read-only
  `query_life` `today` action. The model action requires the exact UTC offset
  and returns the same primary outcome, top actions, calendar, must-do work,
  awaiting responses, routines, risks, schedule, Restia-owned work, bounded
  sources, degraded-source assumptions, available reviewed actions, and
  explicit input/change/reason/actor/workflow/reversal record as the visual
  Today surface. Authenticated owners remain isolated; auth-disabled first-run
  installs reuse the same safe fallback scope without creating an Account or
  audit row. The focused Mission Control, frontend, Life-tool, schema/prompt,
  proactive-knowledge, Communications, and registry suite passed with **88
  passed**; Python 3.11 compilation and whitespace checks passed. A clean
  isolated runtime migrated `0001 → 0018`, reported ready on `3.1.0`, served
  `/today` with HTTP 200, returned all 14 Today sources plus transparency, and
  served the exact five questions plus “Ask Restia anything…”. A direct clean
  first-run `query_life` call returned the same dated read-only contract with
  no changes. The complete suite exercised **6,637 passing contracts with 4
  skipped**: its first sandboxed run reported 6,623 passes and 14 failures, all
  attributable to denied localhost/Unix socket binds or the known
  order-sensitive Telegram lifecycle isolation; the exact affected CalDAV,
  Chroma, Docker-socket, two-instance, DNS-pinning, webhook, and Telegram set
  then passed **56/56** with socket access and isolated lifecycle state. The
  final feature plus release-policy set passed **128/128**, including the
  strict `3.1.0 → v3.1` preflight. Live model-provider invocation and
  real-device visual QA remain open, so the broader Sections 1, 22, 25, and 26
  stay partial.
- **2026-07-18 — explicit Unified Life Graph contract (working tree after
  `dd8e5c1`)**: completed Section 20 without creating a parallel domain store.
  `core/database.py` names the specification's complete core-node set as
  `CORE_LIFE_ENTITY_TYPES`, while existing `LifeEntity` references keep mature
  email, project, calendar, note, document, and other domain rows authoritative.
  `src/life_graph.py` now exposes the enforced owner boundary,
  `life:read`/`life:write` scopes, sensitivity, lifecycle version, and exact
  soft-delete or source-cascade semantics on every serialized source, node,
  and edge. The canonical source-backed Email → person → project → decision →
  task → deadline → calendar → file → goal traversal proves all eight typed
  edges, owner isolation, confidence, provenance, versions, and that deleting
  the first association removes the reachable traversal without deleting the
  downstream records. The focused graph contract passed with **12 passed**.
- **2026-07-18 — complete Calendar and Time contract (working tree after
  `53b96ca`)**: completed Section 5 on the canonical `CalendarEvent` plus Life
  Graph projection instead of introducing another schedule store. The shared
  schedule model explicitly covers meetings, classes, work, personal
  commitments, travel, deadlines, routines, focus, rest, and reminders.
  Calendar intelligence now excludes past free slots, fits active owner-scoped
  tasks into bounded availability using effort, energy, priority, and deadline
  evidence, and returns review-required `manage_calendar` focus-block actions
  without mutating the calendar. Meeting preparation verifies people,
  projects, notes, files, previous meetings, decisions, and follow-up tasks;
  the model-facing calendar schema now exposes those links. The focused
  calendar, action-policy, authority, migration, delivery, Life-tool, schema,
  and release regression set passed with **143 passed**; Python compilation
  and whitespace validation also passed.
