"""Local, Sandbox-only SQLite backup utility for Custom Song orders."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from typing import Any
import uuid


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ORDERS_TABLE = "custom_song_orders"


class SQLiteBackupError(RuntimeError):
    """A safe, actionable error from the local backup utility."""


def _resolve_existing_source(value: str | Path) -> Path:
    source = Path(value).expanduser().resolve(strict=False)
    if not source.is_file():
        raise SQLiteBackupError("Source database must exist and be a regular file.")
    return source.resolve()


def _resolve_destination(value: str | Path, source: Path) -> Path:
    destination = Path(value).expanduser().resolve(strict=False)
    if not destination.is_dir():
        raise SQLiteBackupError("Destination directory must already exist.")
    destination = destination.resolve()
    if destination == source.parent:
        raise SQLiteBackupError("Destination directory must differ from the source database directory.")
    return destination


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _new_temporary_path(destination: Path, suffix: str) -> Path:
    """Reserve a unique temporary file in the destination filesystem."""
    descriptor, filename = tempfile.mkstemp(prefix=".sqlite-backup-", suffix=suffix, dir=destination)
    os.close(descriptor)
    return Path(filename)


def _publish_without_overwrite(temporary: Path, final: Path) -> os.stat_result:
    """Atomically publish a same-filesystem temporary file without replacement.

    os.link creates a new directory entry and fails with FileExistsError if final
    already exists. Unlike rename/replace, it never replaces final on Windows or
    POSIX filesystems that support hard links.
    """
    os.link(temporary, final)
    return final.stat()


def _unlink_owned(path: Path, identity: os.stat_result | None) -> None:
    """Remove only the file this process published, never a later replacement."""
    if identity is None:
        return
    try:
        current = path.stat()
        if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
            path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _application_commit() -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    commit = result.stdout.strip()
    if result.returncode == 0 and len(commit) == 40 and all(character in "0123456789abcdef" for character in commit.lower()):
        return commit
    return None


def _validate_backup(path: Path) -> dict[str, Any]:
    try:
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        try:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or integrity[0] != "ok":
                raise SQLiteBackupError("Backup integrity check failed.")
            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (ORDERS_TABLE,)
            ).fetchone()
            if table is None:
                raise SQLiteBackupError("Backup does not contain custom_song_orders.")
            count = connection.execute(f"SELECT COUNT(*) FROM {ORDERS_TABLE}").fetchone()[0]
            journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
            user_version = connection.execute("PRAGMA user_version").fetchone()[0]
            sqlite_version = connection.execute("SELECT sqlite_version()").fetchone()[0]
        finally:
            connection.close()
    except sqlite3.Error as error:
        raise SQLiteBackupError("Backup validation could not read the SQLite copy.") from error
    return {
        "custom_song_orders_count": count,
        "journal_mode": journal_mode,
        "pragma_user_version": user_version,
        "sqlite_version": sqlite_version,
    }


def _require_sandbox(environment: str) -> None:
    if environment != "sandbox":
        raise SQLiteBackupError("Only environment=sandbox is permitted.")


def _reject_operational_path(value: str | Path) -> Path:
    raw = str(value).replace("\\", "/").lower()
    if raw.startswith("/var/data") or "/live/" in f"/{raw.strip('/')}/":
        raise SQLiteBackupError("Operational or Live paths are not permitted.")
    return Path(value).expanduser().resolve(strict=False)


def verify_backup(*, backup_path: str | Path, manifest_path: str | Path, environment: str) -> dict[str, Any]:
    """Verify an existing Sandbox backup without writing to it or its source DB."""
    _require_sandbox(environment)
    backup = _reject_operational_path(backup_path)
    manifest_file = _reject_operational_path(manifest_path)
    if not backup.is_file() or not manifest_file.is_file():
        raise SQLiteBackupError("Backup and manifest must both exist as regular files.")
    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SQLiteBackupError("Manifest could not be read as JSON.") from error
    required = {"environment", "sha256", "backup_filename", "custom_song_orders_count", "pragma_user_version"}
    if not isinstance(manifest, dict) or not required.issubset(manifest):
        raise SQLiteBackupError("Manifest is incomplete.")
    if manifest["environment"] != "sandbox" or manifest["backup_filename"] != backup.name:
        raise SQLiteBackupError("Backup and manifest environment or filename do not match.")
    if manifest["sha256"] != _sha256(backup):
        raise SQLiteBackupError("Backup checksum does not match the manifest.")
    if "backup_size_bytes" in manifest and manifest["backup_size_bytes"] != backup.stat().st_size:
        raise SQLiteBackupError("Backup size does not match the manifest.")
    details = _validate_backup(backup)
    if details["custom_song_orders_count"] != manifest["custom_song_orders_count"]:
        raise SQLiteBackupError("Backup row count does not match the manifest.")
    if details["pragma_user_version"] != manifest["pragma_user_version"]:
        raise SQLiteBackupError("Backup user version does not match the manifest.")
    return details


def restore_drill(*, backup_path: str | Path, manifest_path: str | Path, destination_db: str | Path, environment: str) -> Path:
    """Create a verified, isolated Sandbox copy. It never replaces a database."""
    verify_backup(backup_path=backup_path, manifest_path=manifest_path, environment=environment)
    backup = _reject_operational_path(backup_path)
    destination = _reject_operational_path(destination_db)
    if destination.exists() or destination == backup:
        raise SQLiteBackupError("Restore-drill destination must be a new, distinct database path.")
    if not destination.parent.is_dir():
        raise SQLiteBackupError("Restore-drill destination directory must exist.")
    temporary: Path | None = None
    published: os.stat_result | None = None
    try:
        temporary = _new_temporary_path(destination.parent, ".restore-drill.tmp")
        source = sqlite3.connect(backup.as_uri() + "?mode=ro", uri=True)
        try:
            target = sqlite3.connect(temporary)
            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()
        details = _validate_backup(temporary)
        manifest_details = verify_backup(backup_path=backup, manifest_path=manifest_path, environment=environment)
        if details["custom_song_orders_count"] != manifest_details["custom_song_orders_count"] or details["pragma_user_version"] != manifest_details["pragma_user_version"]:
            raise SQLiteBackupError("Restore-drill validation did not match the verified backup.")
        published = _publish_without_overwrite(temporary, destination)
        return destination
    except (OSError, sqlite3.Error) as error:
        raise SQLiteBackupError("Could not create the isolated restore drill.") from error
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        # A return only happens after publication; failures never intentionally retain a destination.
        if published is not None and not destination.exists():
            _unlink_owned(destination, published)


def create_backup(*, source_db_path: str | Path, destination_directory: str | Path, environment: str) -> tuple[Path, Path]:
    """Create and validate a local Sandbox backup, then atomically publish it."""
    if environment != "sandbox":
        raise SQLiteBackupError("Only environment=sandbox is permitted.")
    source = _resolve_existing_source(source_db_path)
    destination = _resolve_destination(destination_directory, source)
    backup_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    filename = f"orders-sandbox-{created_at.replace(':', '').replace('-', '')}-{backup_id}.sqlite3"
    backup_path = destination / filename
    manifest_path = backup_path.with_suffix(".json")
    temporary_backup: Path | None = None
    temporary_manifest: Path | None = None
    published_backup_identity: os.stat_result | None = None
    published_manifest_identity: os.stat_result | None = None

    if backup_path.exists() or manifest_path.exists():
        raise SQLiteBackupError("Refusing to overwrite an existing backup artifact.")

    try:
        temporary_backup = _new_temporary_path(destination, ".sqlite3.tmp")
        source_connection = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
        try:
            destination_connection = sqlite3.connect(temporary_backup)
            try:
                source_connection.backup(destination_connection)
            finally:
                destination_connection.close()
        finally:
            source_connection.close()

        details = _validate_backup(temporary_backup)
        manifest = {
            "backup_id": backup_id,
            "created_at_utc": created_at,
            "environment": "sandbox",
            "source_db_path": str(source),
            "backup_filename": filename,
            "sha256": _sha256(temporary_backup),
            "backup_size_bytes": temporary_backup.stat().st_size,
            "sqlite_version": details["sqlite_version"],
            "journal_mode": details["journal_mode"],
            "pragma_user_version": details["pragma_user_version"],
            "custom_song_orders_count": details["custom_song_orders_count"],
            "application_commit": _application_commit(),
        }
        temporary_manifest = _new_temporary_path(destination, ".json.tmp")
        temporary_manifest.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        # Files are finalized only after all copy and validation work succeeds.
        published_backup_identity = _publish_without_overwrite(temporary_backup, backup_path)
        published_manifest_identity = _publish_without_overwrite(temporary_manifest, manifest_path)
        return backup_path, manifest_path
    except (OSError, sqlite3.Error) as error:
        raise SQLiteBackupError("Could not create a consistent SQLite backup.") from error
    finally:
        for temporary in (temporary_backup, temporary_manifest):
            if temporary is None:
                continue
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        # A manifest failure after the DB rename must not leave a published partial backup.
        if published_backup_identity is not None and published_manifest_identity is None:
            _unlink_owned(backup_path, published_backup_identity)


def main(argv: list[str] | None = None, *, stdout=None, stderr=None) -> int:
    parser = argparse.ArgumentParser(description="Create a local, Sandbox-only SQLite orders backup.")
    commands = parser.add_subparsers(dest="command", required=True)
    create_parser = commands.add_parser("create")
    create_parser.add_argument("--source-db", required=True)
    create_parser.add_argument("--destination-directory", required=True)
    create_parser.add_argument("--environment", required=True)
    verify_parser = commands.add_parser("verify")
    verify_parser.add_argument("--backup-db", required=True)
    verify_parser.add_argument("--manifest", required=True)
    verify_parser.add_argument("--environment", required=True)
    drill_parser = commands.add_parser("restore-drill")
    drill_parser.add_argument("--backup-db", required=True)
    drill_parser.add_argument("--manifest", required=True)
    drill_parser.add_argument("--destination-db", required=True)
    drill_parser.add_argument("--environment", required=True)
    args = parser.parse_args(argv)
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    try:
        if args.command == "create":
            backup_path, manifest_path = create_backup(source_db_path=args.source_db, destination_directory=args.destination_directory, environment=args.environment)
            print(f"SQLite Sandbox backup created: {backup_path.name}", file=stdout)
            print(f"Manifest created: {manifest_path.name}", file=stdout)
        elif args.command == "verify":
            verify_backup(backup_path=args.backup_db, manifest_path=args.manifest, environment=args.environment)
            print("SQLite Sandbox backup verification: OK", file=stdout)
        else:
            destination = restore_drill(backup_path=args.backup_db, manifest_path=args.manifest, destination_db=args.destination_db, environment=args.environment)
            print(f"SQLite Sandbox restore drill created: {destination.name}", file=stdout)
    except SQLiteBackupError as error:
        print(f"SQLite Sandbox backup failed: {error}", file=stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
