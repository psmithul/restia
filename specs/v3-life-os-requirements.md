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

- [ ] Public release tag is `v3`; internal package version is `3.0.0`.
- [x] Major/slightly-major/minor public tag precision is enforced in CI.
- [ ] One immutable principal spans web, desktop, mobile, Telegram, API, voice,
      automation, and linked-interface adapters.
- [ ] Profiles and sessions use the shared database; legacy JSON migration is
      idempotent, fail-closed, backed up, and reversible.
- [ ] SQLite private installs and PostgreSQL shared installs pass the same
      domain contract tests.
- [ ] Supabase is verified as an optional PostgreSQL/OIDC deployment adapter,
      not a client-side authorization bypass or mandatory dependency.
- [ ] Every new V3 record is principal-scoped and every mutation is audited.
- [ ] Existing user data upgrades without destructive reset.
- [ ] Backup, readiness, rollback, update scripts, GHCR amd64/arm64 images, and
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

## 3. Today and execution — `partial`

Existing evidence: `/api/mission-control/today`, planning items, project work,
calendar, notes, Study timer, daily briefs, and deterministic next actions.

- [ ] Today shows primary outcome, top three actions, events, must-do tasks,
      people awaiting replies, health/routines, risks/conflicts, suggested
      schedule, and Restia-owned work.
- [ ] Every recommendation states why now, effort, delay cost, linked goal or
      project, source evidence, and what Restia can handle.
- [ ] Focus Mode hides unrelated information, opens context, shows definition
      of done, persists a timer, captures interruptions/progress/evidence, and
      creates follow-ups.

## 4. Goals, projects, milestones, tasks, actions — `partial`

Existing evidence: Projects, stages/work items/checklists, planning items,
Notes goals, Study goals, progression, dependencies, attachments, activity.

- [ ] Life Area → Goal → Project → Milestone → Task → Action is represented
      without duplicating the same commitment across disconnected stores.
- [ ] Tasks include definition of done, priority, deadline, effort, energy,
      context, project, people, dependencies, documents, source, status, next
      action, and completion evidence.
- [ ] Restia detects overdue, blocked, waiting, missing-next-action, irrelevant,
      duplicated, and goal-disconnected tasks.

## 5. Calendar and time — `partial`

Existing evidence: local calendars, Google/CalDAV, recurrence, reminders,
planning-to-calendar scheduling, Today aggregation.

- [ ] Meetings, classes, work, personal commitments, travel, deadlines,
      routines, focus blocks, rest, and reminders share one schedule model.
- [ ] Free-time finding, conflict resolution, time blocking, unfinished-work
      rescheduling, meeting preparation/follow-up, focus protection,
      overcommitment, duration, and energy are verified.
- [ ] Events link people, projects, notes, files, previous meetings, decisions,
      and follow-up tasks.

## 6. Relationship manager — `partial`

Existing evidence: Contacts, direct messaging, profile/status data, Home Link.

- [ ] Person profiles cover relationship, organisation, contact data, origin,
      last interaction, conversations, promises, follow-ups, dates,
      preferences, projects, files, history, and private notes.
- [ ] Follow-up, unanswered-message, project-relevance, and relationship-care
      reminders are source-backed and bounded.
- [ ] No personal message is sent without the required approval.

## 7. Communications hub — `partial`

Existing evidence: Gmail/IMAP email, Telegram, Restia messaging/calls,
notifications, integrations, drafting and email tools.

- [ ] Unified unread/importance view spans enabled communication connectors.
- [ ] Thread summaries, response suggestions, commitments, deadlines, contacts,
      follow-ups, drafts, search, and conversion to life entities are verified.
- [ ] Reading/summarising and drafting policies are distinct from sending.
- [ ] WhatsApp client remains read-only unless a separately approved reply flow
      is implemented; no generic send method exists.

## 8. Personal memory and knowledge — `partial`

Existing evidence: chat history, Restia memory, Mnemosyne, documents, Notes,
personal RAG, Markdown/Obsidian-compatible stores, search.

- [ ] Semantic, episodic, decision, preference, procedural, relationship,
      project, task, and source memory types are explicit.
- [ ] Notes, documents, research, bookmarks, web pages, meetings, writing,
      academic/business records, ideas, and lessons are connected.
- [ ] Markdown can remain a durable source of truth while indexes are
      rebuildable.
- [ ] Answers distinguish confirmed facts, user statements, assumptions,
      inferences, stale information, and gaps, with citations.

## 9. Decisions — `not-started`

- [ ] A Decision stores date, context, options, choice, reasons, risks,
      assumptions, people, evidence, review date, and outcome.
- [ ] Decision recall, assumption review, scheduled review, and change history
      are searchable and source-backed.

## 10. Health and fitness — `not-started`

- [ ] Weight, measurements, sleep, exercise, nutrition, steps, recovery, water,
      medication reminders, appointments, reports, symptoms, mood/stress, and
      wearable data have private typed records and imports.
- [ ] Trends, behavior/outcome links, time-aware workouts, adherence, missed
      routines, appointment questions, and document storage are verified.
- [ ] Medical advice boundaries and urgent-professional-help language are
      enforced; medication is never changed autonomously.

## 11. Personal finance — `not-started`

- [ ] Accounts, cash, cards, income, expenses, subscriptions, investments,
      loans, taxes, bills, receivables, and personal/business scope are modeled.
- [ ] Classification, cash flow, subscriptions, reminders, anomalies, goals,
      receipts, tax documents, net worth, forecasts, and affordability are
      verified.
- [ ] Transfers, investments, cancellations, and other high-risk actions always
      require confirmation.
- [ ] Banking passwords are never stored directly.

## 12. Learning and career — `partial`

Existing evidence: Study Mode, goals/progress/timer/review, documents/research,
Projects.

- [ ] Skills, courses, papers, books, objectives, notes, practice, projects,
      questions, revision, and competency evidence form one learning graph.
- [ ] Roles, companies, universities, applications, resumes, portfolio,
      interviews, contacts, deadlines, gaps, and decisions form one career
      workspace.
- [ ] Career → capability → gap → learning plan → portfolio → weekly action is
      traversable and actionable.

## 13. Work and business workspaces — `partial`

Existing evidence: Projects, project members/links, documents, tasks, activity.

- [ ] Each workspace isolates objectives, projects, people, meetings, tasks,
      documents, decisions, metrics, risks, procedures, communications, and
      activity.
- [ ] Cross-workspace relationships are explicit and never broaden access.

## 14. Habits and routines — `not-started`

- [ ] Morning/evening, workout, meals, review, learning, finance, relationship,
      maintenance, and sleep routines are supported.
- [ ] Tracking covers consistency, quality, friction, failure causes, recovery,
      goal effect, and continued usefulness without meaningless streaks.

## 15. Home and personal administration — `partial`

Existing evidence: Documents, Notes/reminders, files, Calendar.

- [ ] IDs, insurance, warranties, renewals, inventory, repairs, purchases,
      deliveries, vehicles, travel documents, forms, providers, household
      routines, and emergency information are modeled and linked to files.
- [ ] Expiry alerts are verified.

## 16. Travel — `partial`

Existing evidence: calendar events, email receipts/tickets, notes/files.

- [ ] Research, budget, transport, lodging, visas, itinerary, packing,
      reservations, local transport, documents, contacts, expenses, and
      calendar use one trip context.
- [ ] Travel Mode presents only immediate, offline-available information.

## 17. Journal and reflection — `partial`

Existing evidence: Notes, documents, memory, daily brief/task summaries.

- [ ] Journal, mood, moments, wins, difficulties, lessons, gratitude, ideas,
      principles, and periodic reflection have private typed capture.
- [ ] Weekly/monthly/annual reviews cover changes, improvement, repeated
      failure, time, relationships, goal progress, and next changes.

## 18. Automation engine — `partial`

Existing evidence: ScheduledTask, webhooks, email/calendar pollers, task runs,
agents, notifications.

- [ ] Triggers cover time, email, calendar, overdue tasks, uploads, people,
      metric thresholds, location, forms, and project status.
- [ ] Actions cover entity creation, scheduling, database updates, drafts,
      approved sends, briefings, file moves, reports, information requests,
      notifications, agents, and workflows.
- [ ] Meeting-end workflow is verified end to end.
- [ ] Automation execution honors domain autonomy and idempotency.

## 19. Proactive intelligence — `partial`

Existing evidence: Today risks, email urgency, task scheduling, daily briefs,
notification center.

- [ ] Restia detects overdue commitments, goal conflicts, overload, missing
      follow-ups, unanswered messages, unused subscriptions, financial/health
      anomalies, stalled projects, unscheduled deadlines, postponement, and
      stale-decision assumptions.
- [ ] Interruptions are limited to urgent, important, time-sensitive, high-risk,
      or explicitly requested matters; everything else enters a digest.

## 20. Unified life graph — `partial`

- [ ] All required core entity types and owner-scoped typed edges exist.
- [x] Existing domain rows can be referenced without duplicating authority.
- [ ] Source provenance, confidence, permissions, versioning, and deletion
      behavior are explicit.
- [ ] Email → person → project → decision → task → deadline → calendar → file →
      goal is verified as a real traversal.

## 21. Permission and autonomy — `partial`

Existing evidence: profile privileges, confirmations, tool policies, API token
scopes, project roles.

- [ ] Levels 1–6 are represented on every prepared/executed action.
- [ ] Per-domain caps and confirmation rules are configurable.
- [ ] Calendar, drafts, sends, WhatsApp, deletion, finance, legal, and medical
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

## 23. Minimal navigation — `partial`

Existing evidence: declarative navigation registry and V2 shell; current
top-level surface remains broader than the target.

- [ ] Permanent primary navigation is Restia, Today, Inbox, Life, Search.
- [ ] Goals, Projects, People, Health, Money, Learning, Work, Home, Journal, and
      Files are contextual destinations inside Life.
- [ ] Existing deep links, commands, accessibility, mobile behavior, and user
      visibility preferences migrate without loss.

## 24. Ideal home screen — `partial`

- [ ] Home answers what matters now, what is happening today, what needs
      attention, what Restia is handling, and what changed.
- [ ] “Ask Restia anything…” is the primary action.

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
