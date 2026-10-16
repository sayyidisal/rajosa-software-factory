"""CLI for offline SQLite backup / verify (Phase 4H.2).

Usage (from ``apps/api`` with venv active)::

    python -m app.persistence.backup_cli backup \\
      --source var/rajosa.sqlite3 \\
      --dest var/backups/rajosa-YYYYmmdd.sqlite3

    python -m app.persistence.backup_cli verify \\
      --path var/backups/rajosa-YYYYmmdd.sqlite3 \\
      --checksum var/backups/rajosa-YYYYmmdd.sqlite3.sha256

``verify`` requires ``--checksum`` (fail-closed restore gate). Never prints
secret env values. Does not start the API or runners.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.persistence.backup import (
    BackupError,
    create_sqlite_backup,
    default_backup_filename,
    read_checksum_file,
    verify_sqlite_backup,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.persistence.backup_cli",
        description="Offline SQLite backup and verification for Rajosa task DB",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    backup = sub.add_parser("backup", help="Create a consistent SQLite backup")
    backup.add_argument(
        "--source",
        type=Path,
        required=True,
        help="Path to TASK_DB_PATH sqlite file",
    )
    backup.add_argument(
        "--dest",
        type=Path,
        default=None,
        help="Destination .sqlite3 path (must not exist)",
    )
    backup.add_argument(
        "--dest-dir",
        type=Path,
        default=None,
        help="Directory for auto-named backup when --dest is omitted",
    )
    backup.add_argument(
        "--no-checkpoint",
        action="store_true",
        help=(
            "Skip PRAGMA wal_checkpoint(FULL). Not recommended; use only when "
            "a complete checkpoint cannot run and you accept that risk"
        ),
    )

    verify = sub.add_parser(
        "verify",
        help="Verify a backup file (requires --checksum; restore gate)",
    )
    verify.add_argument("--path", type=Path, required=True)
    verify.add_argument(
        "--checksum",
        type=Path,
        required=True,
        help=(
            "Required .sha256 file from backup. Without checksum matching, "
            "this CLI will not treat verification as a complete restore gate"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "backup":
            source = args.source
            if args.dest is not None:
                dest = args.dest
            else:
                dest_dir = args.dest_dir or (source.parent / "backups")
                dest = dest_dir / default_backup_filename(source)
            result = create_sqlite_backup(
                source_path=source,
                dest_path=dest,
                checkpoint=not args.no_checkpoint,
            )
            print(f"backup_ok path={result.backup_path}")
            print(f"checksum_ok path={result.checksum_path} sha256={result.sha256}")
            print(
                f"counts tasks={result.task_count} events={result.event_count} "
                f"migrations={list(result.migration_versions)}"
            )
            return 0

        if args.command == "verify":
            expected = read_checksum_file(args.checksum)
            report = verify_sqlite_backup(
                dest_path=args.path,
                expected_sha256=expected,
            )
            if not report.integrity_ok:
                # Defense in depth: helper raises on integrity failure; still fail closed.
                print(
                    f"error: backup failed integrity_check: {report.integrity_message}",
                    file=sys.stderr,
                )
                return 2
            print(
                f"verify_ok integrity={report.integrity_message} "
                f"sha256={report.sha256} checksum_matches={report.checksum_matches}"
            )
            print(
                f"counts tasks={report.task_count} events={report.event_count} "
                f"migrations={list(report.migration_versions)}"
            )
            print(f"lifecycles={report.lifecycle_counts}")
            return 0
    except BackupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    parser.error(f"unknown command {args.command!r}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
