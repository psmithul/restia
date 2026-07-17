# Restia schema migration foundation

This directory contains Restia's frozen, deterministic schema baseline.

The pushed `20260716_0001` revision remains immutable and stamp-only.
`20260717_0002` creates the full schema on an empty database using explicit
Alembic operations and does not import live ORM metadata. Empty databases stamp
0001 without executing its guard, then upgrade through 0002. Existing local
SQLite installs receive a backed-up compatibility repair before
`scripts/odysseus-db stamp-legacy` verifies every baseline table, critical
security columns and constraints, issuer-qualified identity uniqueness, and
append-only audit guards. Only then does it record 0002 without replaying its
creation DDL over existing tables.

Normal Restia startup is the supported empty-database path because it records
the immutable 0001 boundary before executing 0002. Direct/offline Alembic runs
must use `alembic upgrade 20260716_0001:head`. Shared/PostgreSQL mode still
refuses application startup until remaining SQLite-only runtime state and
distributed-worker ownership have been ported and
`SHARED_SCHEMA_AUTHORITY_READY` is enabled in the same reviewed change.

Useful diagnostics:

```bash
scripts/odysseus-db status --pretty
scripts/odysseus-db stamp-legacy --pretty
alembic current
alembic heads
```
