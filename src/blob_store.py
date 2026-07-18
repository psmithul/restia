"""Validated filesystem blob contract for durable Restia attachments.

SQL is the authority for attachment metadata; bytes deliberately stay outside
the relational database.  Local-single installs keep the historical Restia
directories.  Shared deployments must explicitly opt into a filesystem that
is mounted consistently on every replica.  No provider SDK or credential is
accepted here: adding an object-store backend requires a separate, reviewed
implementation of this contract.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Mapping

from src.constants import DATA_DIR, PROJECT_FILES_DIR, UPLOAD_DIR


LOCAL_FILESYSTEM = "local-filesystem"
SHARED_FILESYSTEM = "shared-filesystem"
SUPPORTED_BLOB_STORES = frozenset({LOCAL_FILESYSTEM, SHARED_FILESYSTEM})


class BlobStoreConfigurationError(RuntimeError):
    """The configured blob store is unsafe or incomplete."""


class BlobKeyError(ValueError):
    """A caller supplied a path-shaped or escaping blob key."""


def _safe_chmod(path: Path, mode: int) -> None:
    if os.name == "nt":
        return
    try:
        os.chmod(path, mode)
    except OSError:
        pass


@dataclass(frozen=True)
class BlobStoreConfig:
    kind: str
    root: Path
    chat_root: Path
    project_root: Path
    shared: bool


def _env_value(environ: Mapping[str, str], primary: str, legacy: str) -> str:
    return str(environ.get(primary) or environ.get(legacy) or "").strip()


def _absolute_root(raw: str, *, setting: str) -> Path:
    candidate = Path(raw)
    if not raw or not candidate.is_absolute():
        raise BlobStoreConfigurationError(
            f"{setting} must be an explicit absolute directory"
        )
    resolved = candidate.resolve(strict=False)
    if resolved == Path(resolved.anchor):
        raise BlobStoreConfigurationError(
            f"{setting} cannot be a filesystem root"
        )
    if resolved.exists() and not resolved.is_dir():
        raise BlobStoreConfigurationError(
            f"{setting} must name a directory"
        )
    return resolved


def resolve_blob_store_config(
    *,
    environ: Mapping[str, str] | None = None,
    database_mode: str | None = None,
    create: bool = False,
) -> BlobStoreConfig:
    """Resolve and validate the one attachment byte-store configuration.

    ``shared`` database mode never infers a pod-local path.  It requires an
    explicit ``RESTIA_BLOB_STORE=shared-filesystem`` and
    ``RESTIA_BLOB_ROOT=/absolute/mount`` contract.  Local mode preserves the
    shipped upload directories unless an explicit local root is supplied.
    """

    env = os.environ if environ is None else environ
    mode = str(
        database_mode
        or env.get("RESTIA_DATABASE_MODE")
        or env.get("ODYSSEUS_DATABASE_MODE")
        or "local-single"
    ).strip()
    requested_kind = _env_value(
        env, "RESTIA_BLOB_STORE", "ODYSSEUS_BLOB_STORE"
    )
    requested_root = _env_value(
        env, "RESTIA_BLOB_ROOT", "ODYSSEUS_BLOB_ROOT"
    )

    if mode == "shared":
        if requested_kind != SHARED_FILESYSTEM:
            raise BlobStoreConfigurationError(
                "shared database mode requires "
                "RESTIA_BLOB_STORE=shared-filesystem"
            )
        root = _absolute_root(requested_root, setting="RESTIA_BLOB_ROOT")
        config = BlobStoreConfig(
            kind=SHARED_FILESYSTEM,
            root=root,
            chat_root=(root / "uploads").resolve(strict=False),
            project_root=(root / "project_files").resolve(strict=False),
            shared=True,
        )
    else:
        kind = requested_kind or LOCAL_FILESYSTEM
        if kind != LOCAL_FILESYSTEM:
            choices = ", ".join(sorted(SUPPORTED_BLOB_STORES))
            raise BlobStoreConfigurationError(
                f"local-single RESTIA_BLOB_STORE must be one of: {choices}"
            )
        if requested_root:
            root = _absolute_root(requested_root, setting="RESTIA_BLOB_ROOT")
            chat_root = (root / "uploads").resolve(strict=False)
            project_root = (root / "project_files").resolve(strict=False)
        else:
            root = Path(DATA_DIR).resolve(strict=False)
            chat_root = Path(UPLOAD_DIR).resolve(strict=False)
            project_root = Path(PROJECT_FILES_DIR).resolve(strict=False)
        config = BlobStoreConfig(
            kind=LOCAL_FILESYSTEM,
            root=root,
            chat_root=chat_root,
            project_root=project_root,
            shared=False,
        )

    for child in (config.chat_root, config.project_root):
        try:
            child.relative_to(config.root)
        except ValueError as exc:
            raise BlobStoreConfigurationError(
                "Blob namespaces must remain beneath RESTIA_BLOB_ROOT"
            ) from exc
        if child == config.root:
            raise BlobStoreConfigurationError(
                "Chat and project blob namespaces must be distinct"
            )
        if child.exists() and not child.is_dir():
            raise BlobStoreConfigurationError(
                "Configured blob namespace must be a directory"
            )

    if config.chat_root == config.project_root:
        raise BlobStoreConfigurationError(
            "Chat and project blob namespaces must be distinct"
        )

    if create:
        for directory in (config.root, config.chat_root, config.project_root):
            directory.mkdir(parents=True, exist_ok=True)
            if not config.shared:
                _safe_chmod(directory, 0o700)
    return config


class FileSystemBlobStore:
    """Atomic, path-confined byte operations beneath one validated root."""

    def __init__(self, root: str | os.PathLike[str], *, create: bool = True):
        raw = str(root or "").strip()
        self.root = _absolute_root(raw, setting="blob root")
        if create:
            self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def validate_key(value: object) -> str:
        raw = str(value or "")
        pure = PurePosixPath(raw)
        if (
            not raw
            or "\\" in raw
            or pure.is_absolute()
            or not pure.parts
            or any(part in {"", ".", ".."} for part in pure.parts)
            or any("\x00" in part for part in pure.parts)
        ):
            raise BlobKeyError("Invalid blob key")
        normalized = pure.as_posix()
        if normalized != raw:
            raise BlobKeyError("Invalid blob key")
        return normalized

    def resolve(self, key: object, *, must_exist: bool = True) -> Path:
        normalized = self.validate_key(key)
        candidate = self.root.joinpath(*PurePosixPath(normalized).parts)
        resolved = candidate.resolve(strict=False)
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise BlobKeyError("Blob key escapes configured root") from exc
        if candidate.is_symlink():
            raise BlobKeyError("Blob key resolves through a symbolic link")
        if must_exist and not resolved.is_file():
            raise FileNotFoundError(normalized)
        return resolved

    def exists(self, key: object) -> bool:
        try:
            return self.resolve(key, must_exist=True).is_file()
        except (BlobKeyError, FileNotFoundError, OSError):
            return False

    def write_stream(self, key: object, source: BinaryIO) -> Path:
        path = self.resolve(key, must_exist=False)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".restia-blob-", dir=path.parent)
        try:
            try:
                os.fchmod(fd, 0o600)
            except (AttributeError, OSError):
                pass
            with os.fdopen(fd, "wb") as handle:
                fd = -1
                source.seek(0)
                while True:
                    chunk = source.read(64 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            _safe_chmod(path, 0o600)
            return path
        finally:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def delete(self, key: object) -> None:
        path = self.resolve(key, must_exist=False)
        path.unlink(missing_ok=True)
        parent = path.parent
        while parent != self.root:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
