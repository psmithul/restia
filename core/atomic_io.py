"""Atomic JSON file writes.

Use this everywhere a JSON config file is persisted. A plain `open("w") +
json.dump` truncates the file on first write and only fills it with new
content afterwards — a kill -9 / power loss / OOM in between produces a
truncated or empty file. For password DBs (`auth.json`) and live state
(`sessions.json`, `settings.json`, `integrations.json`, `cookbook_state.json`),
that's a data-loss event.

`atomic_write_json` writes to a sibling tmp file, fsyncs, then `os.replace`s
into place. On POSIX `os.replace` is atomic on the same filesystem.
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any, Optional


def _write_atomic(path: str, writer, *, mode: int = 0o600) -> None:
    """Write through a private sibling file, then atomically replace ``path``.

    ``mkstemp`` matters here for more than convenience: unlike ``open()`` it
    creates the temporary file with mode 0600 before any secret bytes are
    written and gives concurrent threads/processes distinct names.  The final
    chmod also repairs legacy files that were previously created under a
    permissive umask (auth/session/config JSON can contain credentials).
    """
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=os.path.basename(path) + ".tmp.",
        dir=parent,
        text=True,
    )
    try:
        try:
            os.fchmod(fd, mode)
        except (AttributeError, OSError):
            # Windows protects the per-user data directory with ACLs and may
            # not implement POSIX modes.
            pass
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fd = -1  # fdopen owns it now
            writer(f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        try:
            os.chmod(path, mode)
        except OSError:
            pass
        # Persist the rename itself when the platform supports directory fsync.
        try:
            dir_fd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def atomic_write_json(
    path: str,
    data: Any,
    *,
    indent: Optional[int] = None,
    mode: int = 0o600,
) -> None:
    """Atomically persist `data` as JSON at `path`.

    The temporary file is created securely with a unique sibling name so
    concurrent threads/processes cannot collide on the rename target.
    """
    _write_atomic(path, lambda f: json.dump(data, f, indent=indent), mode=mode)


def atomic_write_text(path: str, text: str, *, mode: int = 0o600) -> None:
    if not isinstance(text, str):
        raise TypeError("atomic_write_text expects a string")
    _write_atomic(path, lambda f: f.write(text), mode=mode)
