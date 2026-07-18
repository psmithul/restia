"""Static startup contracts for singleton shared-runtime loops."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_singleton_app_loops_run_through_database_leadership():
    source = (ROOT / "app.py").read_text(encoding="utf-8")

    assert '"upload-cleanup", upload_cleanup_func' in source
    assert '"null-owner-sweep", _null_owner_sweep_loop' in source
    assert '"home-call-alert-watcher", home_call_alert_watcher' in source
    assert '"nightly-skill-audit", _skill_audit_nightly_loop' in source
    assert source.count("run_database_leased_worker(") >= 4


def test_claim_fenced_delivery_workers_remain_independent_from_task_scheduler():
    source = (ROOT / "app.py").read_text(encoding="utf-8")

    assert "contact_delivery_loop()" in source
    assert "calendar_delivery_loop()" in source
    assert "RESTIA_INPROCESS_CONTACT_DELIVERY" in source
    assert "RESTIA_INPROCESS_CALENDAR_DELIVERY" in source
