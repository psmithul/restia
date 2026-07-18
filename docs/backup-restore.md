# Backup & Restore

Restia keeps all of your state in the `data/` directory — the SQLite database
(`app.db`), the Fernet encryption key (`data/.app_key`), the vault, memory, RAG
indexes, personal documents, Project workspaces and deliverables, and uploads.
The `scripts/odysseus-backup` tool
snapshots that directory into a single gzip tarball and restores it later.

Snapshots are safe to take while the app is running: SQLite databases are copied
through SQLite's own `.backup` API rather than a raw file copy, so an in-flight
write can't corrupt the snapshot.

> **A snapshot contains your secrets.** The tarball includes the Fernet
> encryption key (`data/.app_key`), the vault, sessions, and any stored
> provider/API tokens — so treat it like a password. Store backups somewhere
> private and never commit them to Git. The recurring worker creates only
> AES-256-GCM encrypted archives and verifies each archive before retention.

## Quick start

Run the tool from the repository root:

```bash
# Create an owner-only passphrase file outside data/ (do this once)
mkdir -p secrets && chmod 700 secrets
python3 -c 'import secrets; print(secrets.token_urlsafe(48))' > secrets/backup-passphrase
chmod 600 secrets/backup-passphrase

# Create an encrypted snapshot
./scripts/odysseus-backup snapshot \
  --encrypt-with-passphrase-file secrets/backup-passphrase

# List existing snapshots (most recent first)
./scripts/odysseus-backup list

# Check a tarball's integrity without extracting it
./scripts/odysseus-backup verify backups/odysseus-backup-20260101-120000.tar.gz.restia \
  --passphrase-file secrets/backup-passphrase

# Restore (destructive — see the warning below)
./scripts/odysseus-backup restore backups/odysseus-backup-20260101-120000.tar.gz.restia \
  --passphrase-file secrets/backup-passphrase --yes
```

Plain tar snapshots use the Python standard library. Encrypted archives also
use Restia's pinned `cryptography` dependency, so native encrypted CLI runs
should use the app virtualenv (`.venv/bin/python scripts/odysseus-backup ...`)
or an activated Restia environment. The in-process worker and Docker image
already run in that environment.

Every command prints a JSON result. Add `--pretty` for indented output.

## Commands

### `snapshot`

Writes a `tar.gz` of `data/` to `backups/<timestamp>.tar.gz`.

| Flag | Effect |
| --- | --- |
| `--out PATH` | Write to a specific path instead of the default `backups/` location. Must be **outside** `data/`. |
| `--include-research` | Include `data/deep_research/` (skipped by default — research runs are large). |
| `--include-attachments` | Include `data/mail-attachments/` (skipped by default — cached IMAP extractions, re-derivable). |
| `--encrypt-with-passphrase-file PATH` | Publish an authenticated AES-256-GCM `.restia` archive using an owner-only external passphrase file. |

By default the snapshot includes everything under `data/` **except**
`deep_research/` and `mail-attachments/`. Personal uploads, Project records,
Project deliverables, and documents are included.

```bash
# Snapshot straight to a mounted NAS path
./scripts/odysseus-backup snapshot --out /mnt/nas/odysseus-$(date +%F).tar.gz

# Full snapshot including research runs and mail attachments
./scripts/odysseus-backup snapshot --include-research --include-attachments
```

### `list`

Lists the tarballs in `backups/`, most recent first, with size and modification
time.

### `verify PATH`

Opens the tarball read-only and walks every member to confirm it is intact and
safe to restore. Nothing is extracted. Use this before relying on an old backup
or after copying one across machines. Encrypted archives require
`--passphrase-file PATH`.

### `restore PATH --yes`

Overwrites `data/` from a tarball.

> **Restore is destructive.** It replaces the current `data/` directory. `--yes`
> is required so a mistyped command can't wipe your live state.

Restore is not a blind delete: before extracting, the tool **renames your current
`data/` to `data.before-restore-<timestamp>`** in the repository root. If a
restore turns out to be wrong, your previous state is still there — delete the
restored data directory and rename the sibling stash back. Encrypted archives
require `--passphrase-file PATH`. The restore path is also
validated entry-by-entry: archives containing absolute paths, `..` segments,
symlinks, or anything outside `data/` are rejected.

## Recurring encrypted backups

The application owns an independent database-leased backup worker. It is not a
scheduled Task, so `RESTIA_INPROCESS_TASKS=0` does not disable it. The worker:

1. Creates an encrypted snapshot with no shell interpolation.
2. Decrypts and validates every archive member through `verify`.
3. Records bounded, secret-free run health in canonical SQL.
4. Applies retention only after successful verification.

Enable it for a native install:

```bash
export RESTIA_BACKUP_PASSPHRASE_FILE=/private/path/backup-passphrase
export RESTIA_BACKUP_DIRECTORY=/private/path/restia-backups
export RESTIA_BACKUP_INTERVAL_HOURS=24
export RESTIA_BACKUP_RETENTION_COUNT=14
```

The passphrase file must be a regular non-symlink file, at least 12 bytes, and
owner-only (`chmod 600`) on POSIX. The backup directory must be outside the
Restia data directory. Security Posture reports configuration, the latest
durable run and whether a verified archive is current.

For Docker Compose, place the file at `./secrets/backup-passphrase`, run
`chmod 600 secrets/backup-passphrase`, and set this in `.env`:

```dotenv
RESTIA_BACKUP_PASSPHRASE_FILE=/run/restia-secrets/backup-passphrase
```

Compose mounts `./secrets` read-only at `/run/restia-secrets` and persists
encrypted archives from `/app/backups` in host `./backups`.

Shared PostgreSQL mode fails explicitly with
`shared_operator_backup_required`: a correct shared backup must cover both the
authoritative PostgreSQL database and the shared blob store. Configure an
operator-managed `pg_dump`/snapshot workflow for that deployment.

## Optional external/offsite scheduling

The tarball output composes cleanly with cron and any copy tool. For example, a
nightly encrypted snapshot copied offsite:

```cron
0 3 * * *  cd /path/to/restia && ./scripts/odysseus-backup snapshot --out "/mnt/nas/restia-$(date +\%F).tar.gz.restia" --encrypt-with-passphrase-file /private/path/backup-passphrase
```

Swap the `--out` target for `scp`, `rclone`, `s3cmd`, or similar to push the
snapshot to remote storage.

## Docker vs native installs

The tool honors `RESTIA_DATA_DIR` and `RESTIA_BACKUP_DIRECTORY`, falling back to
`data/` and `backups/` relative to the repository root:

- **Native installs** — run it from the repo root as shown above. `data/` and
  `backups/` are both in the repo directory.
- **Docker** — `docker-compose.yml` bind-mounts host `./data`, `./backups`, and
  read-only `./secrets` to `/app/data`, `/app/backups`, and
  `/run/restia-secrets`. The in-process worker therefore survives container
  recreation. The CLI may also be run on the host against the same directories.

> **ChromaDB caveat (Docker only).** In the Docker setup, ChromaDB stores its
> vectors in a separate Compose-managed volume (declared as `chromadb-data`),
> **not** under `./data`. `odysseus-backup` therefore does not capture the Docker
> ChromaDB store. Back it up separately if you need it. Compose prefixes the
> volume with the project name, so find the real name first
> (`docker volume ls | grep chromadb`), then archive it — for example:
>
> ```bash
> docker run --rm -v <project>_chromadb-data:/data -v "$PWD":/backup \
>   alpine tar czf /backup/chromadb.tar.gz -C /data .
> ```
>
> On native installs ChromaDB lives at `data/chroma/` and is included in the
> snapshot normally.
