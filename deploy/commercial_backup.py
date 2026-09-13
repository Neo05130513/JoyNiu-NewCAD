#!/usr/bin/env python3
"""Offline deployment/data snapshots; restore only into a new staging folder.

Contains sensitive configuration when present. Keep snapshots private/offsite.
This tool never stops services, changes production, or reads container env.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import stat
import subprocess


INCLUDE = ("data", "deploy", "dist", "src", "public", "apps/api/app",
           "apps/api/pyproject.toml", "apps/api/README.md", "compose.yaml", ".env",
           ".dockerignore", "package.json", "package-lock.json", "index.html", "vite.config.js")
SKIP = {"__pycache__", ".pytest_cache", ".cache", "backups", "node_modules", ".venv", ".git"}
SQLITE_SUFFIXES = {".sqlite", ".sqlite3", ".db"}
EXCLUDED_DIRECTORIES = {
    "deploy/.secrets/codex/tmp": "Codex regenerates this runtime temporary directory, including process wrapper symlinks; authentication and configuration outside it are preserved.",
}
EXCLUDED_FILES = {
    "data/operations/admission.lock": "Transient coordinated-backup OS lock; recreated by the API at startup.",
    "data/operations/maintenance.json": "Transient coordinated-backup admission marker; never restore a maintenance interruption.",
}


def _digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _files(root):
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("Symlinks are not supported in an offline snapshot")
        if path.is_file():
            yield path


def _copy(source, destination, *, skip_names=SKIP, exclude_directories=(), exclude_files=()):
    if source.is_symlink():
        raise ValueError("Symlinks are not supported in a deployment backup")
    if source in exclude_files:
        if not source.is_file():
            raise ValueError("A configured runtime-file exclusion is not a file")
        return
    if source in exclude_directories:
        if not source.is_dir():
            raise ValueError("A configured temporary-directory exclusion is not a directory")
        return
    if source.is_dir():
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        for item in source.iterdir():
            if item.name not in skip_names:
                _copy(item, destination / item.name, skip_names=set() if item.name == ".secrets" else skip_names,
                      exclude_directories=exclude_directories, exclude_files=exclude_files)
    elif source.is_file():
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copy2(source, destination)
    else:
        raise ValueError("Special files are not supported in a deployment backup")


def _check_db(path):
    # All callers use immutable backup/staging databases, never live databases.
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("SQLite integrity verification failed")
        return {row[0]: db.execute('SELECT count(*) FROM "' + row[0].replace('"', '""') + '"').fetchone()[0]
                for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")}


def backup(root, destination, *, quiescent=False):
    root, destination = Path(root).resolve(), Path(destination).resolve()
    if not quiescent:
        raise ValueError("Stop all application writers before a cross-database snapshot")
    if destination == root or root in destination.parents:
        raise ValueError("Backup destination must be outside the deployment tree")
    if not (root / "data").is_dir():
        raise ValueError("Deployment data directory does not exist")
    destination.mkdir(parents=True, mode=0o700, exist_ok=False)
    os.chmod(destination, 0o700)
    payload = destination / "snapshot"
    payload.mkdir(mode=0o700)
    exclude_directories = {root / relative for relative in EXCLUDED_DIRECTORIES}
    try:
        for name in INCLUDE:
            source = root / name
            if source.exists() or source.is_symlink():
                _copy(source, payload / name, skip_names=set() if name == "data" else SKIP,
                      exclude_directories=exclude_directories, exclude_files={root / path for path in EXCLUDED_FILES})
        databases = {}
        # Raw copies alone lose data held in WAL. Replace every database with
        # SQLite's online-backup API image, then remove copied journal files.
        for source in _files(root / "data"):
            if source.suffix.lower() not in SQLITE_SUFFIXES:
                continue
            with source.open("rb") as handle:
                is_database = handle.read(16) == b"SQLite format 3\x00"
            if not is_database:
                continue
            relative = source.relative_to(root)
            target = payload / relative
            temporary = target.with_name(target.name + ".consistent")
            with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as read_db:
                with closing(sqlite3.connect(temporary)) as write_db:
                    read_db.backup(write_db)
                    write_db.execute("PRAGMA journal_mode=DELETE")
            os.chmod(temporary, stat.S_IMODE(source.stat().st_mode))
            temporary.replace(target)
            for suffix in ("-wal", "-shm", "-journal"):
                (target.parent / (target.name + suffix)).unlink(missing_ok=True)
            databases[relative.as_posix()] = _check_db(target)
        manifest = {"format": "joyniu-commercial-offline-v1", "createdAt": datetime.now(timezone.utc).isoformat(),
                    "quiescent": True, "databases": databases,
                    "exclusions": [{"path": relative, "reason": reason} for relative, reason in EXCLUDED_DIRECTORIES.items()
                                   if (root / relative).is_dir()] + [
                                       {"path": relative, "reason": reason} for relative, reason in EXCLUDED_FILES.items()
                                       if (root / relative).is_file()],
                    "directories": sorted(path.relative_to(payload).as_posix() for path in payload.rglob("*") if path.is_dir()),
                    "files": {path.relative_to(payload).as_posix(): {"sha256": _digest(path), "size": path.stat().st_size}
                              for path in _files(payload)}}
        manifest_path = destination / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        os.chmod(manifest_path, 0o600)
        return {"files": len(manifest["files"]), "databases": len(databases)}
    except Exception:
        # An incomplete snapshot is retained for diagnosis but never has a
        # usable manifest. It cannot be restored as if the backup succeeded.
        (destination / "manifest.json").unlink(missing_ok=True)
        raise


def verify(backup_directory):
    root = Path(backup_directory).resolve()
    payload = root / "snapshot"
    if payload.is_symlink() or not payload.is_dir():
        raise ValueError("Invalid snapshot directory")
    manifest = json.loads((root / "manifest.json").read_text())
    if manifest.get("format") != "joyniu-commercial-offline-v1" or manifest.get("quiescent") is not True:
        raise ValueError("Unsupported or incomplete snapshot")
    paths = manifest.get("files")
    if not isinstance(paths, dict):
        raise ValueError("Invalid snapshot manifest")
    actual = {path.relative_to(payload).as_posix() for path in _files(payload)}
    if actual != set(paths):
        raise ValueError("Snapshot file inventory does not match")
    for name in manifest.get("directories", []):
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts or not (payload / name).is_dir():
            raise ValueError("Invalid snapshot directory inventory")
    for name, value in paths.items():
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Invalid snapshot path")
        path = payload / name
        if path.stat().st_size != value["size"] or _digest(path) != value["sha256"]:
            raise ValueError("Snapshot checksum does not match")
    for name, counts in manifest["databases"].items():
        if name not in paths or _check_db(payload / name) != counts:
            raise ValueError("Snapshot database does not match its manifest")
    return manifest


def restore(backup_directory, destination):
    manifest = verify(backup_directory)
    destination = Path(destination).resolve()
    if destination.exists():
        raise ValueError("Restore destination must not exist; production is never overwritten")
    source = Path(backup_directory).resolve() / "snapshot"
    destination.mkdir(parents=True, mode=0o700)
    os.chmod(destination, 0o700)
    for name in manifest.get("directories", []):
        (destination / name).mkdir(parents=True, exist_ok=True, mode=0o700)
    for path in _files(source):
        target = destination / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copy2(path, target)
    for name, counts in manifest["databases"].items():
        if _check_db(destination / name) != counts:
            raise ValueError("Restored database failed verification")
    return {"files": len(manifest["files"]), "databases": len(manifest["databases"])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    snapshot = commands.add_parser("backup")
    snapshot.add_argument("--root", required=True)
    snapshot.add_argument("--destination", required=True)
    snapshot.add_argument("--writers-stopped", action="store_true", help="Explicitly attest all application writers are stopped")
    for name in ("verify", "restore"):
        command = commands.add_parser(name)
        command.add_argument("--backup", required=True)
        if name == "restore":
            command.add_argument("--destination", required=True)
    args = parser.parse_args()
    try:
        if args.command == "backup":
            if not args.writers_stopped:
                raise ValueError("--writers-stopped is required after stopping application writers")
            # Refuse a known running Compose API even if the flag was passed.
            check = subprocess.run(["docker", "compose", "ps", "--status", "running", "--services"], cwd=args.root,
                                   capture_output=True, text=True, check=True, timeout=15)
            if "api" in check.stdout.split():
                raise ValueError("Compose API is still running; snapshot refused")
            result = backup(args.root, args.destination, quiescent=True)
        elif args.command == "restore":
            result = restore(args.backup, args.destination)
        else:
            value = verify(args.backup)
            result = {"files": len(value["files"]), "databases": len(value["databases"])}
        print(json.dumps({"ok": True, **result}))
    except Exception:
        # Commands/configs can contain credentials; no exception body or
        # captured Compose output is echoed into terminal or deploy logs.
        print(json.dumps({"ok": False, "message": "Backup/restore check failed; inspect the protected snapshot locally."}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
