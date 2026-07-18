"""Tests for the readiness / integrity self-check (src/readiness.py)."""

from cryptography.fernet import Fernet

from src.readiness import check_readiness


def test_readiness_reports_core_subsystems():
    result = check_readiness()

    assert {"ready", "version", "checks", "timestamp"}.issubset(result.keys())
    checks = result["checks"]
    for name in (
        "database_mode", "database", "schema", "shared_runtime",
        "data_dir", "local_first",
    ):
        assert name in checks, f"missing check: {name}"

    # In the dev/test environment the local SQLite DB and data dir are present,
    # so the critical checks must pass and overall readiness must be True.
    assert checks["database"]["ok"] is True, checks["database"]
    assert checks["schema"]["ok"] is True, checks["schema"]
    assert checks["shared_runtime"]["ok"] is True, checks["shared_runtime"]
    assert checks["data_dir"]["ok"] is True, checks["data_dir"]
    assert result["ready"] is True, result


def test_local_first_check_is_informational_never_fatal():
    result = check_readiness()
    lf = result["checks"]["local_first"]
    # local_first reports whether storage stays on-host but must never gate
    # readiness — a remote database is a valid deployment.
    assert lf["ok"] is True
    assert "local" in lf


def test_shared_mode_readiness_accepts_authority_but_rejects_stale_binding(
    monkeypatch,
):
    monkeypatch.setenv("RESTIA_DATABASE_MODE", "shared")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+psycopg://restia:private@db/restia",
    )
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("LOCALHOST_BYPASS", "false")
    monkeypatch.setenv(
        "RESTIA_ENCRYPTION_KEY",
        Fernet.generate_key().decode("ascii"),
    )
    monkeypatch.setenv("RESTIA_BLOB_STORE", "shared-filesystem")
    monkeypatch.setenv("RESTIA_BLOB_ROOT", "/mnt/restia-blobs")

    result = check_readiness()

    assert result["ready"] is False
    assert result["checks"]["shared_runtime"] == {
        "ok": True,
        "critical": True,
        "schema_authority_ready": True,
        "blocker_codes": [],
    }
    assert result["checks"]["database_mode"]["ok"] is False
    assert result["checks"]["database_mode"]["code"] == (
        "database_mode_binding_mismatch"
    )
    assert "private" not in str(result)
