"""Explicit local compatibility adapter for the retired uploads.json index.

Production Restia uses SQL metadata.  This adapter exists for bounded legacy
import and for isolated compatibility callers/tests that deliberately supply a
non-production upload root.  It is never selected by the production factory.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any


class LegacyUploadIndexAdapter:
    def __init__(self, upload_dir: str | os.PathLike[str]):
        self.upload_dir = Path(upload_dir).resolve(strict=False)
        self.lock = threading.Lock()
        self._cache: dict[str, Any] | None = None
        self._mtime = 0.0

    @property
    def path(self) -> Path:
        return self.upload_dir / "uploads.json"

    def write(self, path: str | os.PathLike[str], data: dict) -> None:
        target = Path(path)
        directory = target.parent
        fd, temporary = tempfile.mkstemp(
            prefix=".uploads-", suffix=".tmp", dir=directory
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            if target.exists():
                try:
                    shutil.copy2(target, str(target) + ".bak")
                except OSError:
                    pass
            os.replace(temporary, target)
            if target.name == "uploads.json":
                self._cache = data
                try:
                    self._mtime = target.stat().st_mtime
                except OSError:
                    self._mtime = time.time()
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def load(self) -> dict[str, Any]:
        target = self.path
        if not target.exists():
            self._cache = {}
            self._mtime = 0.0
            return {}
        try:
            mtime = target.stat().st_mtime
            if self._cache is not None and mtime <= self._mtime:
                return self._cache
        except OSError:
            mtime = 0.0
        for candidate in (target, Path(str(target) + ".bak")):
            if not candidate.exists():
                continue
            try:
                value = json.loads(candidate.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    self._cache = value
                    self._mtime = mtime
                    return value
            except Exception:
                continue
        self._cache = {}
        return {}
