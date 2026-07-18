"""Independent recurring encrypted-backup lifecycle.

The scheduler is deployment authority, not a user Task. It runs under the
database leadership lease used by other singleton runtime workers, invokes the
reviewed backup CLI without a shell, verifies each encrypted archive before it
is eligible for retention, and stores only secret-free run evidence in SQL.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable, Mapping, Sequence

from core.database import SessionLocal, utcnow_naive
from src.backup_encryption import BackupEncryptionError, inspect_encrypted_backup
from src.backup_models import BackupRun
from src.runtime_paths import get_app_root, get_default_data_dir


log = logging.getLogger(__name__)

DEFAULT_INTERVAL_HOURS = 24.0
DEFAULT_RETENTION_COUNT = 14
DEFAULT_TIMEOUT_SECONDS = 3_600.0
DEFAULT_POLL_SECONDS = 300.0
MAX_COMMAND_OUTPUT_BYTES = 128 * 1024
MIN_PASSPHRASE_BYTES = 12
MAX_PASSPHRASE_FILE_BYTES = 64 * 1024


class BackupSchedulerError(RuntimeError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = str(code)[:64]
        self.detail = str(detail)[:500]


@dataclass(frozen=True, slots=True)
class BackupScheduleConfig:
    interval_seconds: float
    retention_count: int
    timeout_seconds: float
    poll_seconds: float
    passphrase_file: Path
    backup_dir: Path
    data_dir: Path
    database_mode: str


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes


CommandRunner = Callable[[Sequence[str], Mapping[str, str], float], Awaitable[CommandResult]]


def _truthy(value: object, *, default: bool = True) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() not in {"", "0", "false", "no", "off"}


def _number(
    env: Mapping[str, str],
    name: str,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    raw = str(env.get(name, default)).strip()
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise BackupSchedulerError(
            "invalid_backup_configuration", f"{name} must be numeric"
        ) from exc
    if not minimum <= value <= maximum:
        raise BackupSchedulerError(
            "invalid_backup_configuration",
            f"{name} must be between {minimum:g} and {maximum:g}",
        )
    return value


def _runtime_path(value: str | None, default: Path) -> Path:
    candidate = Path(value).expanduser() if value else default
    if not candidate.is_absolute():
        candidate = Path(get_app_root()) / candidate
    # Keep the final path component lexical so lstat-based symlink rejection
    # below cannot be bypassed by resolving the link before validation.
    return Path(os.path.abspath(candidate))


def _database_mode(env: Mapping[str, str]) -> str:
    value = str(
        env.get("RESTIA_DATABASE_MODE")
        or env.get("ODYSSEUS_DATABASE_MODE")
        or "local-single"
    ).strip().lower()
    if value not in {"local-single", "shared"}:
        raise BackupSchedulerError(
            "invalid_backup_configuration", "Unsupported database mode"
        )
    return value


def _validate_passphrase_file(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise BackupSchedulerError(
            "backup_passphrase_unavailable",
            "Backup passphrase file is not readable",
        ) from exc
    if path.is_symlink() or not stat.S_ISREG(info.st_mode):
        raise BackupSchedulerError(
            "backup_passphrase_invalid",
            "Backup passphrase file must be a regular non-symlink file",
        )
    if info.st_size > MAX_PASSPHRASE_FILE_BYTES:
        raise BackupSchedulerError(
            "backup_passphrase_invalid", "Backup passphrase file is too large"
        )
    if os.name != "nt" and stat.S_IMODE(info.st_mode) & 0o077:
        raise BackupSchedulerError(
            "backup_passphrase_permissions",
            "Backup passphrase file must be owner-only",
        )
    try:
        secret = path.read_bytes().rstrip(b"\r\n")
    except OSError as exc:
        raise BackupSchedulerError(
            "backup_passphrase_unavailable",
            "Backup passphrase file is not readable",
        ) from exc
    if len(secret) < MIN_PASSPHRASE_BYTES:
        raise BackupSchedulerError(
            "backup_passphrase_invalid",
            f"Backup passphrase must contain at least {MIN_PASSPHRASE_BYTES} bytes",
        )


def _validate_backup_directory(directory: Path, data_dir: Path) -> None:
    directory_resolved = directory.resolve(strict=False)
    data_resolved = data_dir.resolve(strict=False)
    if directory_resolved == data_resolved or data_resolved in directory_resolved.parents:
        raise BackupSchedulerError(
            "backup_directory_invalid",
            "Backup directory must be outside the Restia data directory",
        )
    if directory.is_symlink():
        raise BackupSchedulerError(
            "backup_directory_invalid", "Backup directory must not be a symbolic link"
        )
    try:
        directory.mkdir(parents=True, exist_ok=True)
        if not directory.is_dir():
            raise OSError("not a directory")
        if os.name != "nt":
            os.chmod(directory, 0o700)
    except OSError as exc:
        raise BackupSchedulerError(
            "backup_directory_unavailable", "Backup directory is not writable"
        ) from exc


def backup_runtime_status(
    environ: Mapping[str, str] | None = None,
) -> dict[str, object]:
    env = os.environ if environ is None else environ
    interval_raw = str(env.get("RESTIA_BACKUP_INTERVAL_HOURS", DEFAULT_INTERVAL_HOURS)).strip()
    passphrase_configured = bool(str(env.get("RESTIA_BACKUP_PASSPHRASE_FILE") or "").strip())
    inprocess = _truthy(
        env.get("RESTIA_INPROCESS_BACKUPS", env.get("ODYSSEUS_INPROCESS_BACKUPS")),
        default=True,
    )
    try:
        interval = float(interval_raw)
        interval_enabled = interval > 0
    except ValueError:
        interval_enabled = False
    enabled = bool(inprocess and interval_enabled and passphrase_configured)
    if not inprocess:
        reason = "inprocess_disabled"
    elif not interval_enabled:
        reason = "interval_disabled_or_invalid"
    elif not passphrase_configured:
        reason = "passphrase_file_not_configured"
    else:
        reason = "configured"
    return {
        "supported": True,
        "enabled": enabled,
        "configured": passphrase_configured,
        "inprocess": inprocess,
        "reason": reason,
    }


def inprocess_backup_scheduler_enabled(
    environ: Mapping[str, str] | None = None,
) -> bool:
    return bool(backup_runtime_status(environ)["enabled"])


def load_backup_schedule_config(
    environ: Mapping[str, str] | None = None,
) -> BackupScheduleConfig:
    env = os.environ if environ is None else environ
    if not inprocess_backup_scheduler_enabled(env):
        raise BackupSchedulerError(
            "backup_scheduler_disabled",
            "Recurring encrypted backups are not fully configured",
        )
    interval_hours = _number(
        env, "RESTIA_BACKUP_INTERVAL_HOURS", DEFAULT_INTERVAL_HOURS,
        minimum=1 / 60, maximum=24 * 365,
    )
    retention = int(_number(
        env, "RESTIA_BACKUP_RETENTION_COUNT", DEFAULT_RETENTION_COUNT,
        minimum=1, maximum=365,
    ))
    timeout_seconds = _number(
        env, "RESTIA_BACKUP_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS,
        minimum=30, maximum=86_400,
    )
    poll_seconds = _number(
        env, "RESTIA_BACKUP_POLL_SECONDS", DEFAULT_POLL_SECONDS,
        minimum=1, maximum=3_600,
    )
    data_dir = _runtime_path(
        env.get("RESTIA_DATA_DIR") or env.get("ODYSSEUS_DATA_DIR"),
        Path(get_default_data_dir()),
    )
    backup_dir = _runtime_path(
        env.get("RESTIA_BACKUP_DIRECTORY")
        or env.get("ODYSSEUS_BACKUP_DIRECTORY"),
        Path(get_app_root()) / "backups",
    )
    passphrase_file = _runtime_path(
        str(env.get("RESTIA_BACKUP_PASSPHRASE_FILE") or ""), Path("."),
    )
    _validate_passphrase_file(passphrase_file)
    _validate_backup_directory(backup_dir, data_dir)
    return BackupScheduleConfig(
        interval_seconds=interval_hours * 3_600,
        retention_count=retention,
        timeout_seconds=timeout_seconds,
        poll_seconds=min(poll_seconds, interval_hours * 3_600),
        passphrase_file=passphrase_file,
        backup_dir=backup_dir,
        data_dir=data_dir,
        database_mode=_database_mode(env),
    )


async def _subprocess_runner(
    command: Sequence[str], env: Mapping[str, str], timeout_seconds: float,
) -> CommandResult:
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=dict(env),
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=timeout_seconds,
        )
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.communicate()
        raise BackupSchedulerError(
            "backup_command_timeout", "Backup command exceeded its time limit"
        ) from exc
    return CommandResult(
        returncode=int(process.returncode or 0),
        stdout=stdout[:MAX_COMMAND_OUTPUT_BYTES],
        stderr=stderr[:MAX_COMMAND_OUTPUT_BYTES],
    )


def _json_result(result: CommandResult, *, stage: str) -> dict[str, object]:
    if result.returncode != 0:
        raise BackupSchedulerError(
            f"backup_{stage}_failed", f"Backup {stage} command failed"
        )
    try:
        value = json.loads(result.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupSchedulerError(
            f"backup_{stage}_invalid_output",
            f"Backup {stage} command returned invalid output",
        ) from exc
    if not isinstance(value, dict) or value.get("ok") is not True:
        raise BackupSchedulerError(
            f"backup_{stage}_invalid_output",
            f"Backup {stage} command did not confirm success",
        )
    return value


def _command_environment(config: BackupScheduleConfig) -> dict[str, str]:
    env = dict(os.environ)
    env["RESTIA_DATA_DIR"] = str(config.data_dir)
    env["RESTIA_BACKUP_DIRECTORY"] = str(config.backup_dir)
    env["RESTIA_DATABASE_MODE"] = config.database_mode
    return env


def _record_start(session_factory, *, config: BackupScheduleConfig, trigger: str) -> BackupRun:
    row = BackupRun(
        id=str(uuid.uuid4()),
        run_key=uuid.uuid4().hex,
        trigger=trigger,
        database_mode=config.database_mode,
        state="running",
        archive_name=None,
        archive_bytes=None,
        encrypted=False,
        verified=False,
        error_code=None,
        error_detail=None,
        started_at=utcnow_naive(),
        completed_at=None,
    )
    db = session_factory()
    try:
        db.add(row)
        db.commit()
        db.refresh(row)
        return row
    finally:
        db.close()


def _record_success(
    session_factory, *, run_id: str, archive_name: str, archive_bytes: int,
) -> BackupRun:
    db = session_factory()
    try:
        row = db.query(BackupRun).filter(BackupRun.id == run_id).one()
        row.state = "completed"
        row.archive_name = archive_name
        row.archive_bytes = int(archive_bytes)
        row.encrypted = True
        row.verified = True
        row.error_code = None
        row.error_detail = None
        row.completed_at = utcnow_naive()
        db.commit()
        db.refresh(row)
        return row
    finally:
        db.close()


def _record_failure(
    session_factory, *, run_id: str, error: BackupSchedulerError,
) -> BackupRun:
    db = session_factory()
    try:
        row = db.query(BackupRun).filter(BackupRun.id == run_id).one()
        row.state = "failed"
        row.archive_name = None
        row.archive_bytes = None
        row.encrypted = False
        row.verified = False
        row.error_code = error.code
        row.error_detail = error.detail
        row.completed_at = utcnow_naive()
        db.commit()
        db.refresh(row)
        return row
    finally:
        db.close()


def _prune_verified_archives(config: BackupScheduleConfig, *, keep: Path) -> int:
    candidates: list[Path] = []
    for item in config.backup_dir.glob("restia-backup-*.tar.gz.restia"):
        try:
            if item.is_symlink() or not item.is_file():
                continue
            candidates.append(item)
        except OSError:
            continue
    candidates.sort(key=lambda item: (item.stat().st_mtime_ns, item.name), reverse=True)
    retained = set(candidates[: config.retention_count]) | {keep}
    removed = 0
    for item in candidates:
        if item in retained:
            continue
        try:
            item.unlink()
            removed += 1
        except OSError:
            log.warning("Could not prune an expired encrypted backup archive")
    return removed


async def run_scheduled_backup_once(
    *,
    config: BackupScheduleConfig | None = None,
    session_factory=SessionLocal,
    command_runner: CommandRunner = _subprocess_runner,
    trigger: str = "scheduled",
) -> BackupRun:
    resolved = config or load_backup_schedule_config()
    run = _record_start(session_factory, config=resolved, trigger=trigger)
    destination = resolved.backup_dir / (
        "restia-backup-"
        + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        + f"-{run.run_key[:8]}.tar.gz.restia"
    )
    try:
        if resolved.database_mode == "shared":
            raise BackupSchedulerError(
                "shared_operator_backup_required",
                "Shared PostgreSQL and blob storage require an operator-managed backup",
            )
        script = Path(get_app_root()) / "scripts" / "odysseus-backup"
        environment = _command_environment(resolved)
        snapshot = await command_runner((
            sys.executable,
            str(script),
            "snapshot",
            "--out",
            str(destination),
            "--encrypt-with-passphrase-file",
            str(resolved.passphrase_file),
        ), environment, resolved.timeout_seconds)
        snapshot_payload = _json_result(snapshot, stage="snapshot")
        if snapshot_payload.get("encrypted") is not True:
            raise BackupSchedulerError(
                "backup_snapshot_unencrypted",
                "Backup snapshot did not confirm authenticated encryption",
            )
        verify = await command_runner((
            sys.executable,
            str(script),
            "verify",
            str(destination),
            "--passphrase-file",
            str(resolved.passphrase_file),
        ), environment, resolved.timeout_seconds)
        verify_payload = _json_result(verify, stage="verify")
        if verify_payload.get("encrypted") is not True:
            raise BackupSchedulerError(
                "backup_verify_unencrypted",
                "Backup verification did not confirm authenticated encryption",
            )
        info = inspect_encrypted_backup(destination)
        row = _record_success(
            session_factory,
            run_id=run.id,
            archive_name=destination.name,
            archive_bytes=info.encrypted_size,
        )
        _prune_verified_archives(resolved, keep=destination)
        return row
    except asyncio.CancelledError:
        raise
    except BackupSchedulerError as exc:
        destination.unlink(missing_ok=True)
        return _record_failure(session_factory, run_id=run.id, error=exc)
    except (BackupEncryptionError, OSError, ValueError) as exc:
        destination.unlink(missing_ok=True)
        safe = BackupSchedulerError(
            "backup_verification_failed",
            f"Backup verification failed ({exc.__class__.__name__})",
        )
        return _record_failure(session_factory, run_id=run.id, error=safe)
    except Exception as exc:
        destination.unlink(missing_ok=True)
        safe = BackupSchedulerError(
            "backup_runtime_failed",
            f"Backup runtime failed ({exc.__class__.__name__})",
        )
        return _record_failure(session_factory, run_id=run.id, error=safe)


def latest_backup_run(session_factory=SessionLocal) -> dict[str, object] | None:
    db = session_factory()
    try:
        row = db.query(BackupRun).order_by(
            BackupRun.started_at.desc(), BackupRun.id.desc(),
        ).first()
        if row is None:
            return None
        return {
            "state": row.state,
            "trigger": row.trigger,
            "database_mode": row.database_mode,
            "archive_name": row.archive_name,
            "archive_bytes": row.archive_bytes,
            "encrypted": bool(row.encrypted),
            "verified": bool(row.verified),
            "error_code": row.error_code,
            "started_at": row.started_at.isoformat() + "Z",
            "completed_at": (
                row.completed_at.isoformat() + "Z" if row.completed_at else None
            ),
        }
    finally:
        db.close()


def _next_delay(config: BackupScheduleConfig, session_factory) -> float:
    db = session_factory()
    try:
        row = db.query(BackupRun).order_by(
            BackupRun.started_at.desc(), BackupRun.id.desc(),
        ).first()
    finally:
        db.close()
    if row is None:
        return 0.0
    now = utcnow_naive()
    interval = config.interval_seconds
    if row.state == "failed":
        interval = min(interval, 900.0)
    due = row.started_at + timedelta(seconds=interval)
    return max(0.0, (due - now).total_seconds())


async def backup_scheduler_loop(
    *,
    session_factory=SessionLocal,
    command_runner: CommandRunner = _subprocess_runner,
) -> None:
    config = load_backup_schedule_config()
    while True:
        delay = _next_delay(config, session_factory)
        if delay <= 0:
            row = await run_scheduled_backup_once(
                config=config,
                session_factory=session_factory,
                command_runner=command_runner,
            )
            if row.state == "failed":
                log.error("Recurring encrypted backup failed: %s", row.error_code)
            continue
        await asyncio.sleep(min(config.poll_seconds, delay))


__all__ = [
    "BackupScheduleConfig",
    "BackupSchedulerError",
    "CommandResult",
    "backup_runtime_status",
    "backup_scheduler_loop",
    "inprocess_backup_scheduler_enabled",
    "latest_backup_run",
    "load_backup_schedule_config",
    "run_scheduled_backup_once",
]
