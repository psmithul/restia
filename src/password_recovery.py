"""Local-owner proof for self-hosted password recovery.

Restia profiles do not require an email address, so email-only recovery would
lock existing installations out.  Instead, each data directory owns a random
recovery key protected by the same filesystem boundary as the auth database.
The key rotates after every successful use.
"""

from __future__ import annotations

import argparse
import hmac
import os
import secrets
import stat
import threading
from pathlib import Path
from typing import Callable

from src.constants import DATA_DIR


RECOVERY_KEY_PATH = Path(DATA_DIR) / ".password_recovery_key"
_KEY_BYTES = 24
_recovery_lock = threading.Lock()


def _new_key() -> str:
    return secrets.token_urlsafe(_KEY_BYTES)


def _write_key(path: Path, value: str, *, exclusive: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    target = path
    if not exclusive:
        target = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(target, flags, stat.S_IRUSR | stat.S_IWUSR)
    try:
        os.write(fd, (value + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        if not exclusive:
            os.replace(target, path)
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    finally:
        if target != path:
            target.unlink(missing_ok=True)


def ensure_recovery_key(path: Path | None = None) -> Path:
    """Create the install recovery key once, without replacing an existing key."""

    path = path or RECOVERY_KEY_PATH
    with _recovery_lock:
        if not path.exists():
            try:
                _write_key(path, _new_key(), exclusive=True)
            except FileExistsError:
                pass
        if not path.is_file() or path.is_symlink():
            raise RuntimeError("Password recovery key path is not a regular file")
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        return path


def use_recovery_key(
    supplied_key: str,
    operation: Callable[[], bool],
    *,
    path: Path | None = None,
) -> bool:
    """Run ``operation`` once for a valid key, then rotate that key.

    Validation, the password transaction, and rotation are serialized so two
    concurrent requests cannot reuse the same recovery key.
    """

    path = path or RECOVERY_KEY_PATH
    with _recovery_lock:
        try:
            expected = path.read_text(encoding="utf-8").strip()
        except OSError:
            return False
        candidate = str(supplied_key or "").strip()
        if not expected or not hmac.compare_digest(candidate, expected):
            return False
        if not operation():
            return False
        _write_key(path, _new_key(), exclusive=False)
        return True


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.password_recovery",
        description="Show the local Restia password-recovery key.",
    )
    parser.add_argument("command", choices=("show",), nargs="?", default="show")
    return parser


def main() -> int:
    _build_parser().parse_args()
    path = ensure_recovery_key()
    print(f"Recovery key file: {path}")
    print(path.read_text(encoding="utf-8").strip())
    print("This key rotates after a successful password reset. Keep it private.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
