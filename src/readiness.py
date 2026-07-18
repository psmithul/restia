"""Ithaca anchor — local-instance readiness / integrity self-check.

Beyond ``/api/health``'s liveness ping, this confirms the self-hosted instance is
whole and at home: the database is reachable, the data directory is present and
writable, and storage is local-first. Served by ``GET /api/ready`` and suitable
for an orchestrator readiness probe (200 only when every critical check passes).
"""

import os
import uuid
from datetime import datetime, timezone
from typing import Dict


def check_readiness() -> Dict[str, object]:
    """Run the readiness checks and return a JSON-serialisable report.

    ``ready`` is True only when every critical check (database, data_dir) passes.
    ``local_first`` is informational — a remote database is a valid deployment, so
    it never fails readiness, it only reports whether storage stays on this host.
    """
    from core.constants import APP_VERSION, DATA_DIR
    from core.database import engine
    from sqlalchemy import text as sql_text
    from src.database_migrations import schema_revision_status
    from src.database_runtime import (
        SHARED_SCHEMA_AUTHORITY_READY,
        shared_runtime_blockers,
        validate_database_mode,
    )

    checks: Dict[str, Dict[str, object]] = {}

    try:
        runtime = validate_database_mode(require_schema_authority=False)
        binding_matches = runtime.dialect == str(engine.dialect.name)
        checks["database_mode"] = {
            "ok": binding_matches,
            "critical": True,
            "mode": runtime.mode,
            "dialect": runtime.dialect,
            "driver": runtime.driver,
            "schema_authority": runtime.schema_authority,
        }
        if not binding_matches:
            checks["database_mode"]["code"] = "database_mode_binding_mismatch"
    except Exception as exc:
        runtime = None
        checks["database_mode"] = {
            "ok": False,
            "critical": True,
            "code": f"database_mode_{type(exc).__name__.lower()}",
        }

    # Database reachable — the simplest honest probe that the engine is live.
    try:
        with engine.connect() as conn:
            conn.execute(sql_text("SELECT 1"))
        checks["database"] = {"ok": True, "critical": True}
    except Exception as e:
        checks["database"] = {
            "ok": False,
            "critical": True,
            "code": f"database_{type(e).__name__.lower()}",
        }

    try:
        revision = schema_revision_status(engine)
        checks["schema"] = {
            "ok": revision.matches_expected,
            "critical": True,
            **revision.as_dict(),
        }
    except Exception as exc:
        checks["schema"] = {
            "ok": False,
            "critical": True,
            "code": f"schema_{type(exc).__name__.lower()}",
        }

    blockers = shared_runtime_blockers() if runtime is not None and runtime.shared else []
    shared_ok = bool(
        runtime is None
        or not runtime.shared
        or (SHARED_SCHEMA_AUTHORITY_READY and not blockers)
    )
    checks["shared_runtime"] = {
        "ok": shared_ok,
        "critical": True,
        "schema_authority_ready": SHARED_SCHEMA_AUTHORITY_READY,
        "blocker_codes": [str(blocker["code"]) for blocker in blockers],
    }

    # Data directory present and writable — home must be able to hold its own data.
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        probe = os.path.join(DATA_DIR, f".ready_probe_{uuid.uuid4().hex}")
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("ok")
        os.remove(probe)
        checks["data_dir"] = {"ok": True, "critical": True, "path": DATA_DIR}
    except Exception as e:
        checks["data_dir"] = {
            "ok": False,
            "critical": True,
            "code": f"data_dir_{type(e).__name__.lower()}",
        }

    # Local-first: storage stays on the home machine (informational, never fatal).
    host = str(getattr(engine.url, "host", "") or "").lower()
    local_first = engine.dialect.name == "sqlite" or host in {
        "localhost", "127.0.0.1", "::1",
    }
    checks["local_first"] = {
        "ok": True,
        "critical": False,
        "local": local_first,
    }

    ready = all(
        bool(check.get("ok"))
        for check in checks.values()
        if bool(check.get("critical", True))
    )
    return {
        "ready": ready,
        "version": APP_VERSION,
        "checks": checks,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
