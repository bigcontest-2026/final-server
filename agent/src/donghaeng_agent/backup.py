"""Same-host, quiescent SQLite recovery snapshots; no institutional RPO/RTO claim.

Usage: python -m donghaeng_agent.backup backup --business-db PATH
       --checkpoint-db PATH --output NEW_DIRECTORY
       python -m donghaeng_agent.backup restore --backup PATH --output NEW_DIRECTORY

Hashes detect corruption, not malicious replacement. Protect backup-directory
access and storage encryption according to deployment policy.
"""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3

from .repository import InvestigationRepository, now

FORMAT = "hermes-sqlite-recovery-1.0"
FILES = ("business.sqlite3", "checkpoints.sqlite3")


class BackupError(ValueError):
    pass


def _readonly(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=15)


def _digest(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return {"sha256": digest.hexdigest(), "bytes": Path(path).stat().st_size}


def _check_database(path):
    with closing(_readonly(path)) as db:
        if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise BackupError("database_integrity_failed")
        if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise BackupError("database_foreign_key_check_failed")


def _check_pair(directory):
    """Paused or queued review work must retain its accompanying checkpoint."""
    with closing(_readonly(Path(directory) / FILES[0])) as business, closing(_readonly(Path(directory) / FILES[1])) as checkpoints:
        try:
            threads = business.execute("""SELECT e.graph_thread_id FROM executions e
                WHERE e.status='interrupted' OR EXISTS (
                    SELECT 1 FROM jobs j WHERE j.execution_id=e.execution_id
                    AND j.action='resume' AND j.status='queued'
                )""").fetchall()
            for (thread_id,) in threads:
                if not checkpoints.execute("SELECT 1 FROM checkpoints WHERE thread_id=? LIMIT 1", (thread_id,)).fetchone():
                    raise BackupError("checkpoint_pair_mismatch")
        except sqlite3.DatabaseError:
            raise BackupError("checkpoint_pair_mismatch") from None


def _new_directory(path):
    directory = Path(path)
    if not directory.parent.is_dir():
        raise BackupError("destination_parent_missing")
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError:
        raise BackupError("destination_already_exists") from None
    return directory


def create_backup(repository, checkpoint_path, destination):
    checkpoint_path = Path(checkpoint_path)
    if not checkpoint_path.is_file():
        raise BackupError("checkpoint_database_missing")
    if checkpoint_path.samefile(repository.path):
        raise BackupError("business_and_checkpoint_must_be_separate")
    directory = _new_directory(destination)
    try:
        # All worker checkpoint writes acquire this same business DB lock first.
        # Use a separate read connection for backup: backing up the connection
        # holding BEGIN IMMEDIATE can wait forever on its own write transaction.
        with repository.connection(write=True, check_job=False) as fence:
            if fence.execute("SELECT 1 FROM jobs WHERE status='running' LIMIT 1").fetchone():
                raise BackupError("running_jobs_must_stop_before_backup")
            for source, filename in ((repository.path, FILES[0]), (checkpoint_path, FILES[1])):
                with closing(_readonly(source)) as original, closing(sqlite3.connect(directory / filename)) as snapshot:
                    original.backup(snapshot)
                    snapshot.execute("PRAGMA journal_mode=DELETE")
                (directory / filename).chmod(0o600)
                _check_database(directory / filename)
            _check_pair(directory)
            manifest = {"format": FORMAT, "created_at": now(), "scope": "same_host_developer_recovery",
                        "consistency": "business_write_fence_no_running_jobs",
                        "files": {name: _digest(directory / name) for name in FILES}}
            (directory / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return {"backup_directory": str(directory.resolve()), **manifest}
    except BaseException:
        # The directory was exclusively created by this invocation, never reused.
        shutil.rmtree(directory)
        raise


def _validate_backup(directory):
    directory = Path(directory)
    try:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise BackupError("backup_manifest_invalid") from None
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT or not isinstance(manifest.get("files"), dict) or set(manifest["files"]) != set(FILES):
        raise BackupError("backup_manifest_invalid")
    for name in FILES:
        path = directory / name
        if path.is_symlink() or not path.is_file():
            raise BackupError("backup_file_missing_or_symlink")
        if manifest["files"][name] != _digest(path):
            raise BackupError("backup_hash_mismatch")
        try:
            _check_database(path)
        except sqlite3.DatabaseError:
            raise BackupError("database_integrity_failed") from None
    _check_pair(directory)
    return manifest


def restore_backup(backup_directory, destination):
    backup_directory = Path(backup_directory)
    manifest = _validate_backup(backup_directory)
    directory = _new_directory(destination)
    try:
        for name in FILES:
            shutil.copyfile(backup_directory / name, directory / name)
            (directory / name).chmod(0o600)
        (directory / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        # Recheck copied bytes too, so source mutation during copy cannot slip past
        # the preflight hashes. Only a completely verified pair is returned.
        _validate_backup(directory)
        return {"directory": str(directory.resolve()), "business_db": str((directory / FILES[0]).resolve()),
                "checkpoint_db": str((directory / FILES[1]).resolve()), "format": FORMAT}
    except BaseException:
        shutil.rmtree(directory)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("backup")
    backup.add_argument("--business-db", required=True, type=Path)
    backup.add_argument("--checkpoint-db", required=True, type=Path)
    backup.add_argument("--output", required=True, type=Path)
    restore = commands.add_parser("restore")
    restore.add_argument("--backup", required=True, type=Path)
    restore.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "backup":
            if not arguments.business_db.is_file():
                raise BackupError("business_database_missing")
            result = create_backup(InvestigationRepository(arguments.business_db), arguments.checkpoint_db, arguments.output)
        else:
            result = restore_backup(arguments.backup, arguments.output)
    except (BackupError, OSError, sqlite3.Error) as exc:
        parser.exit(1, (str(exc) if isinstance(exc, BackupError) else "backup_storage_error") + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
