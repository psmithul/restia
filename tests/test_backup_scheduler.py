from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import Base
from src.backup_encryption import inspect_encrypted_backup
from src.backup_models import BackupRun
from src.backup_scheduler import (
    BackupScheduleConfig,
    BackupSchedulerError,
    CommandResult,
    backup_runtime_status,
    inprocess_backup_scheduler_enabled,
    load_backup_schedule_config,
    run_scheduled_backup_once,
)


@pytest.fixture()
def backup_env(tmp_path):
    data_dir = tmp_path / "data"
    backup_dir = tmp_path / "backups"
    data_dir.mkdir()
    backup_dir.mkdir()
    passphrase = tmp_path / "backup-passphrase"
    passphrase.write_text("correct horse battery staple", encoding="utf-8")
    passphrase.chmod(0o600)
    engine = create_engine(
        f"sqlite:///{data_dir / 'app.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    (data_dir / "private-note.txt").write_text(
        "private scheduled backup evidence", encoding="utf-8",
    )
    config = BackupScheduleConfig(
        interval_seconds=86_400,
        retention_count=2,
        timeout_seconds=120,
        poll_seconds=60,
        passphrase_file=passphrase,
        backup_dir=backup_dir,
        data_dir=data_dir,
        database_mode="local-single",
    )
    yield config, factory
    engine.dispose()


@pytest.mark.asyncio
async def test_scheduled_backup_encrypts_verifies_records_and_prunes(backup_env):
    config, factory = backup_env
    for index in range(3):
        old = config.backup_dir / f"restia-backup-2020010{index}-old.tar.gz.restia"
        old.write_bytes(b"expired")
        os.utime(old, (index + 1, index + 1))

    row = await run_scheduled_backup_once(
        config=config, session_factory=factory,
    )

    assert row.state == "completed"
    assert row.encrypted is True
    assert row.verified is True
    archive = config.backup_dir / row.archive_name
    info = inspect_encrypted_backup(archive)
    assert info.encrypted_size == row.archive_bytes
    assert b"private scheduled backup evidence" not in archive.read_bytes()
    assert len(list(config.backup_dir.glob("restia-backup-*.tar.gz.restia"))) == 2

    with factory() as db:
        stored = db.query(BackupRun).filter_by(id=row.id).one()
        assert stored.state == "completed"
        assert stored.error_code is None


@pytest.mark.asyncio
async def test_failed_and_shared_backups_record_safe_durable_health(backup_env):
    config, factory = backup_env

    async def failed_runner(command, env, timeout):
        return CommandResult(1, b"", b"credential=/private/secret")

    failed = await run_scheduled_backup_once(
        config=config,
        session_factory=factory,
        command_runner=failed_runner,
    )
    assert failed.state == "failed"
    assert failed.error_code == "backup_snapshot_failed"
    assert "private" not in str(failed.error_detail).lower()

    called = False

    async def forbidden_runner(command, env, timeout):
        nonlocal called
        called = True
        return CommandResult(0, b"{}", b"")

    shared = await run_scheduled_backup_once(
        config=replace(config, database_mode="shared"),
        session_factory=factory,
        command_runner=forbidden_runner,
    )
    assert shared.state == "failed"
    assert shared.error_code == "shared_operator_backup_required"
    assert called is False


def test_scheduler_configuration_is_explicit_and_passphrase_is_hardened(
    tmp_path, monkeypatch,
):
    data = tmp_path / "data"
    backups = tmp_path / "backups"
    data.mkdir()
    secret = tmp_path / "secret"
    secret.write_text("long enough backup passphrase", encoding="utf-8")
    secret.chmod(0o644)
    monkeypatch.setenv("RESTIA_DATA_DIR", str(data))
    monkeypatch.setenv("RESTIA_BACKUP_DIRECTORY", str(backups))
    monkeypatch.setenv("RESTIA_BACKUP_PASSPHRASE_FILE", str(secret))

    assert inprocess_backup_scheduler_enabled() is True
    assert backup_runtime_status()["reason"] == "configured"
    with pytest.raises(BackupSchedulerError, match="owner-only"):
        load_backup_schedule_config()

    secret.chmod(0o600)
    config = load_backup_schedule_config()
    assert config.data_dir == data
    assert config.backup_dir == backups

    if os.name != "nt":
        linked_secret = tmp_path / "linked-secret"
        linked_secret.symlink_to(secret)
        monkeypatch.setenv(
            "RESTIA_BACKUP_PASSPHRASE_FILE", str(linked_secret),
        )
        with pytest.raises(BackupSchedulerError, match="non-symlink"):
            load_backup_schedule_config()

    monkeypatch.setenv("RESTIA_INPROCESS_BACKUPS", "0")
    assert inprocess_backup_scheduler_enabled() is False


def test_backup_worker_is_independent_from_user_tasks():
    root = Path(__file__).resolve().parents[1]
    source = (root / "app.py").read_text(encoding="utf-8")
    scheduler = (root / "src" / "backup_scheduler.py").read_text(encoding="utf-8")
    assert "encrypted-backup-scheduler" in source
    assert "run_database_leased_worker" in source
    assert "TaskScheduler" not in scheduler
    assert "ScheduledTask" not in scheduler
