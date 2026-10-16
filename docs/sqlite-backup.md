# SQLite backup and restore (Phase 4H.2 / 4H.3)

Offline operator procedure for the Rajosa task database (`TASK_DB_PATH`, default `apps/api/var/rajosa.sqlite3`).

This database uses **`PRAGMA journal_mode = WAL`**. A plain copy of only the main `.sqlite3` file while the API is running (or without checkpointing) is **not** a reliable backup. Prefer the helper below, which requires a complete `PRAGMA wal_checkpoint(FULL)` (unless explicitly skipped) then SQLite’s online **backup API**.

No RPO/RTO is claimed. Workspace task artifacts under `WORKSPACE_ROOT` are **not** included; back them up separately if needed.

## Preconditions

- Single API process topology (see architecture docs).
- Prefer **stopping** the API (`make dev-api` / uvicorn) so application work is quiescent and `wal_checkpoint(FULL)` can finish (`busy=0`).
- Do not enable the execution runner for backup/restore.
- Keep backups off shared world-readable locations; helpers attempt `0600` file mode where the OS allows.

## Backup

From `apps/api` with the project venv:

```bash
# Recommended: stop the API first, then:
.venv/bin/python -m app.persistence.backup_cli backup \
  --source var/rajosa.sqlite3 \
  --dest-dir var/backups
```

Or choose an explicit destination (must not already exist):

```bash
.venv/bin/python -m app.persistence.backup_cli backup \
  --source /absolute/path/to/rajosa.sqlite3 \
  --dest /absolute/path/to/backups/rajosa-manual.sqlite3
```

What you get:

- One self-contained `.sqlite3` backup file (do **not** rely on copying `-wal` / `-shm` alongside a live main file).
- Adjacent `.sha256` checksum (`<hex>  <filename>`), written **only after** backup + structural verification succeed.
- Printed task/event counts and migration versions.

If backup or verification fails mid-way, the helper removes the incomplete destination it created (it never deletes a pre-existing destination path). Treat a `.sqlite3` **without** a matching `.sha256` as incomplete — do not restore it.

Retention: keep multiple dated copies under `var/backups/` (or an absolute backup directory); prune manually. No scheduler or remote upload is provided.

### WAL / SHM notes

| Situation | Guidance |
|---|---|
| API stopped, then helper backup | Preferred. Complete checkpoint (`busy=0`) + backup API. |
| API still running | Checkpoint may fail with `busy≠0` (helper errors). Stop API and retry. `--no-checkpoint` skips the fold and is **not** a claim that WAL was fully applied. |
| Manual `cp rajosa.sqlite3` only | **Unsupported** while WAL is active. |
| Leftover `rajosa.sqlite3-wal` / `-shm` after stop | Normal; a successful checkpoint folds committed pages before backup. Do not treat orphan `-wal` as the backup. |

## Verify (required before restore)

Checksum is **required** for the CLI restore gate:

```bash
.venv/bin/python -m app.persistence.backup_cli verify \
  --path var/backups/rajosa-XXXX.sqlite3 \
  --checksum var/backups/rajosa-XXXX.sqlite3.sha256
```

Exit code must be `0`. Non-zero means **do not restore**.

Checks:

1. SHA-256 matches the `.sha256` file (content integrity vs that digest — **not** cryptographic authenticity or third-party trust).
2. `PRAGMA integrity_check` returns `ok` (**structural** SQLite integrity only — not full application schema compatibility beyond required tables/migrations present, and **not** semantic lifecycle validity).
3. Required tables exist (`schema_migrations`, `tasks`, `task_events`) and at least one migration row is present.
4. Reports task/event counts and lifecycle histogram (**read-only**; no planner/implementer/runner).

## Restore (validate first, replace carefully)

1. **Never** restore by overwriting the live DB in place as the first step.
2. Run **verify with `--checksum`** (above) until exit `0`.
3. Optionally inspect lifecycles read-only via verify output or:
   ```bash
   sqlite3 file:var/backups/rajosa-XXXX.sqlite3?mode=ro \
     "SELECT lifecycle, COUNT(*) FROM tasks GROUP BY lifecycle;"
   ```
4. To replace the active database:
   - Stop the API.
   - Move/rename the current `TASK_DB_PATH` aside (keep it until smoke checks pass).
   - Copy the **verified** backup to `TASK_DB_PATH` (or point `TASK_DB_PATH` at the verified file).
   - Start the API once; confirm `GET /health` and an authenticated task read.
5. Startup recovery may mark interruptible mid-flight lifecycles as `failed` (`interrupted`). Waiting states are preserved. **No auto-resume** of planner, implementer, or runner.

## Permissions

- Restrict backup directories to the service account / developer user.
- Helpers set owner read/write on backup + checksum when `chmod` is available.

## Isolated restore drill (Phase 4H.3)

Automated offline coverage lives in `apps/api/tests/test_sqlite_restore_drill.py`. That suite:

1. Seeds a **disposable** database via `SqliteTaskRepository` (real schema + migration `001`).
2. Creates a backup and verifies checksum + structural integrity.
3. Materializes a restore copy to a **separate** path (`materialize_verified_restore`) — never overwrites an existing destination and never targets the live `TASK_DB_PATH`.
4. Opens the restore result read-only and checks migrations, tasks, append-only events, and lifecycle counts.
5. Proves bad checksum / corrupt payloads are rejected **before** a restore file is created.
6. Proves failed restore attempts leave the source DB and a valid backup untouched.
7. Confirms the restore path does not newly import planner/implementer adapters or the Docker runner.

Operator practice (still offline, still disposable paths):

```bash
# After a successful verify of a backup file:
.venv/bin/python - <<'PY'
from pathlib import Path
from app.persistence.backup import materialize_verified_restore

materialize_verified_restore(
    backup_path=Path("var/backups/YOUR-BACKUP.sqlite3"),
    checksum_path=Path("var/backups/YOUR-BACKUP.sqlite3.sha256"),
    restore_path=Path("var/backups/restore-drill-readonly.sqlite3"),
)
print("ok")
PY
```

Use a path under `var/backups/` (or another scratch directory) — **not** `var/rajosa.sqlite3`. Delete the drill copy when finished.

**Claim boundary:** passing disposable restore drills proves helper + runbook mechanics. It does **not** prove production recovery, RPO/RTO, multi-process safety, or that a live cutover was executed.

## Limitations

- Not multi-worker safe; not a substitute for authentication or network hardening.
- Does not backup Docker images, `.env` secrets, or workspace files.
- Does not encrypt backups; treat files as sensitive (task plans/outputs).
- Failed checksum, missing checksum, or integrity_check ⇒ do not restore that file.
- No claimed RPO/RTO; no scheduled or remote backup service.
- No automatic restore HTTP endpoint; restore remains an offline operator procedure.
- `create_sqlite_backup` refuses an existing adjacent `.sha256` (or `.sha256.tmp`) so cleanup cannot delete orphan checksum files it did not create.
