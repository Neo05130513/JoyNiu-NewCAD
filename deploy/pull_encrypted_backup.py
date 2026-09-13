#!/usr/bin/env python3
"""One-time verified offsite copy; never creates a production backup or schedule.

Pull defaults to the arguments --host --identity --backup --destination --key-file.
Keys and ciphertext must use distinct private directories. Preserve the key
separately: losing it makes the AES-256-GCM archive unrecoverable.
Use `verify --source FILE --key-file KEY` for authenticated restore verification,
or `restore ... --destination NEW_DIRECTORY` to restore an isolated deployment.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import importlib.util
import inspect
import json
import os
from pathlib import Path, PurePosixPath
import re
import selectors
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

DEFAULT_MAX_BYTES = 8 * 1024**3
MAX_FILES = 250_000
MANIFEST_LIMIT = 16 * 1024**2
CHUNK = 1024**2
MAGIC = b"JOYNIU-OFFSITE-AES256GCM-V1\0"

_spec = importlib.util.spec_from_file_location("_commercial_snapshot", Path(__file__).with_name("commercial_backup.py"))
snapshot = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(snapshot)


def canonical_name(name):
    if not isinstance(name, str) or not name or "\\" in name or "\x00" in name:
        raise ValueError("Invalid archive path")
    value = PurePosixPath(name)
    if value.is_absolute() or ".." in value.parts or value.as_posix() != name or name == ".":
        raise ValueError("Invalid archive path")
    return name


def strict_verify(root, max_bytes):
    """Validate full tree and the producer's checkpointed application DB scope."""
    root = Path(root).absolute()
    if any(path.is_symlink() for path in (root, *root.parents)) or not root.is_dir():
        raise ValueError("Backup path must not contain symlinks")
    roots = {path.name for path in root.iterdir()}
    if roots not in ({"manifest.json", "snapshot"}, {"manifest.json", "snapshot", "managed-backup.json"}):
        raise ValueError("Backup must contain only its manifest and snapshot")
    manifest_path = root / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file() or manifest_path.stat().st_size > MANIFEST_LIMIT:
        raise ValueError("Invalid manifest file")
    if "managed-backup.json" in roots:
        marker_path = root / "managed-backup.json"
        if marker_path.is_symlink() or not marker_path.is_file() or marker_path.stat().st_size > 16384:
            raise ValueError("Invalid managed backup marker")
        marker = json.loads(marker_path.read_text())
        if (not isinstance(marker, dict) or marker.get("format") != "joyniu-operations-v1"
                or marker.get("name") != root.name or not re.fullmatch(r"scheduled-\d{8}T\d{6}Z-[a-f0-9]{8}", root.name)
                or not isinstance(marker.get("deployment"), str) or not Path(marker["deployment"]).is_absolute()
                or marker.get("manifestSha256") != snapshot._digest(manifest_path)):
            raise ValueError("Managed backup marker does not match its manifest")
        if not isinstance(marker.get("createdAt"), str) or datetime.fromisoformat(marker["createdAt"].replace("Z", "+00:00")).tzinfo is None:
            raise ValueError("Invalid managed backup creation time")
    manifest = json.loads(manifest_path.read_text())
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), dict) or not isinstance(manifest.get("databases"), dict):
        raise ValueError("Invalid manifest inventory")
    if not isinstance(manifest.get("directories"), list):
        raise ValueError("Missing directory inventory")
    payload = root / "snapshot"
    if payload.is_symlink() or not payload.is_dir():
        raise ValueError("Invalid snapshot")
    files, directories, databases = set(), set(), set()
    total, members = manifest_path.stat().st_size, 0
    for path in payload.rglob("*"):
        members += 1
        if members > MAX_FILES:
            raise ValueError("Too many backup files")
        mode = path.lstat().st_mode
        name = canonical_name(path.relative_to(payload).as_posix())
        if stat.S_ISDIR(mode):
            directories.add(name)
        elif stat.S_ISREG(mode):
            files.add(name)
            total += path.stat().st_size
            if total > max_bytes:
                raise ValueError("Backup exceeds size limit")
            with path.open("rb") as handle:
                sqlite_header = handle.read(16) == b"SQLite format 3\x00"
            # commercial_backup snapshots/checkpoints data/*.sqlite|sqlite3|db.
            # Credential/runtime files outside this scope remain hash-verified
            # opaque payloads, matching the producer's existing contract.
            if sqlite_header and name.startswith("data/") and path.suffix.lower() in snapshot.SQLITE_SUFFIXES:
                databases.add(name)
                if any(path.with_name(path.name + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
                    raise ValueError("SQLite backup contains live journal files")
        else:
            raise ValueError("Symlinks and special files are forbidden")
    if files != set(manifest["files"]) or directories != set(manifest["directories"]) or len(directories) != len(manifest["directories"]):
        raise ValueError("Manifest does not describe the complete tree")
    if databases != set(manifest["databases"]):
        raise ValueError("Manifest does not describe every SQLite database")
    for name, item in manifest["files"].items():
        canonical_name(name)
        if not isinstance(item, dict) or type(item.get("size")) is not int or item["size"] < 0 or not isinstance(item.get("sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", item["sha256"]):
            raise ValueError("Invalid manifest file metadata")
    # Baseline verifier checks format, quiescence, every digest/size, quick_check
    # and exact table row counts with immutable read-only SQLite connections.
    return snapshot.verify(root)


def private_parent(path):
    path = Path(path).expanduser().absolute()
    if path.exists() or path.is_symlink():
        raise ValueError("Destination already exists")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent = path.parent.resolve()
    info = parent.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("Destination parent must be owned by you with mode 0700")
    return parent / path.name


def exclusive_file(path, data=None):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags, 0o600), "wb") as handle:
        if data is not None:
            handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def read_key(path):
    path = Path(path).expanduser().absolute()
    if path.is_symlink():
        raise ValueError("Key must not be a symlink")
    info, parent = path.stat(), path.parent.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) != 0o700:
        raise ValueError("Key requires a private 0700 directory and mode 0600")
    with os.fdopen(os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)), "rb") as handle:
        key = handle.read(33)
    if len(key) != 32:
        raise ValueError("Invalid AES-256 key")
    return key


def unpack_verified(archive, destination, max_bytes):
    if archive.stat().st_size > max_bytes:
        raise ValueError("Archive exceeds size limit")
    destination.mkdir(mode=0o700, exist_ok=False)
    seen, total = set(), 0
    with tarfile.open(archive, mode="r:") as source:
        for member in source:
            name = canonical_name(member.name)
            if name in seen or len(seen) >= MAX_FILES or member.sparse is not None:
                raise ValueError("Duplicate, sparse or excessive archive entries")
            seen.add(name)
            if name != "manifest.json" and name != "snapshot" and not name.startswith("snapshot/"):
                raise ValueError("Archive contains an unexpected root")
            if not (member.isdir() or member.isreg()):
                raise ValueError("Archive links and special files are forbidden")
            if member.size < 0 or (member.isdir() and member.size != 0):
                raise ValueError("Invalid archive entry size")
            total += member.size
            if total > max_bytes or (name == "manifest.json" and member.size > MANIFEST_LIMIT):
                raise ValueError("Extracted data exceeds size limit")
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if member.isdir():
                target.mkdir(exist_ok=True, mode=0o700)
                if not target.is_dir():
                    raise ValueError("Archive directory collision")
            else:
                with source.extractfile(member) as incoming:
                    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                    with os.fdopen(os.open(target, flags, 0o600), "wb") as outgoing:
                        remaining = member.size
                        while remaining:
                            block = incoming.read(min(CHUNK, remaining))
                            if not block:
                                raise ValueError("Truncated archive file")
                            outgoing.write(block)
                            remaining -= len(block)
    return strict_verify(destination, max_bytes)


def remote_script():
    # Send only local trusted verifier code. No path or host is interpolated
    # into Python code; SSH's remote shell receives separately quoted argv.
    baseline = Path(snapshot.__file__).read_text()
    return ("import sys,os,json,re,stat,tarfile\nfrom pathlib import Path,PurePosixPath\nfrom types import SimpleNamespace\n"
            + "from datetime import datetime\n" + f"MAX_FILES={MAX_FILES}\nMANIFEST_LIMIT={MANIFEST_LIMIT}\n"
            + "scope={'__name__':'_trusted_snapshot_verifier'}\n"
            + f"exec(compile({baseline!r},'<snapshot-verifier>','exec'),scope)\nsnapshot=SimpleNamespace(**scope)\n"
            + inspect.getsource(canonical_name) + "\n" + inspect.getsource(strict_verify)
            + "\ntry:\n root=Path(sys.argv[1]); limit=int(sys.argv[2]); strict_verify(root,limit)\n"
            + " with tarfile.open(fileobj=sys.stdout.buffer,mode='w|',format=tarfile.PAX_FORMAT,dereference=False) as archive:\n"
            + "  for path in [root/'manifest.json',root/'snapshot',*sorted((root/'snapshot').rglob('*'))]:\n"
            + "   archive.add(path,arcname=path.relative_to(root).as_posix(),recursive=False)\n"
            + "except Exception:\n sys.stderr.write('Remote backup validation/streaming failed.\\n'); sys.exit(1)\n")


def ssh_command(host, identity, backup, max_bytes, sudo=False):
    if not isinstance(host, str) or host.startswith("-") or not re.fullmatch(r"(?:[A-Za-z0-9_.-]+@)?[A-Za-z0-9][A-Za-z0-9.-]*", host):
        raise ValueError("Invalid explicit SSH host")
    if not isinstance(backup, str) or not backup.startswith("/") or "\x00" in backup:
        raise ValueError("Remote backup must be an explicit absolute path")
    identity = Path(identity).expanduser().resolve(strict=True)
    remote = (["sudo", "-n"] if sudo else []) + ["python3", "-", backup, str(max_bytes)]
    return ["ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=15",
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2", "-i", str(identity), "--", host, shlex.join(remote)]


def download_archive(command, destination, max_bytes, timeout_seconds):
    with tempfile.TemporaryFile(mode="w+b") as script:
        script.write(remote_script().encode()); script.seek(0)
        exclusive_file(destination)
        with destination.open("wb") as output:
            process = subprocess.Popen(command, stdin=script, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            deadline, size = time.monotonic() + timeout_seconds, 0
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0 or not selector.select(remaining):
                            raise TimeoutError("Backup download timed out")
                        block = os.read(process.stdout.fileno(), CHUNK)
                        if not block:
                            break
                        size += len(block)
                        if size > max_bytes:
                            raise ValueError("Downloaded archive exceeds size limit")
                        output.write(block)
                if process.wait(timeout=max(.1, deadline - time.monotonic())) != 0:
                    raise ValueError("Remote verification or transfer failed")
                output.flush(); os.fsync(output.fileno())
            finally:
                if process.poll() is None:
                    process.kill(); process.wait()
                process.stdout.close()


def encrypt_archive(archive, destination, key):
    nonce = os.urandom(12)
    header = MAGIC + nonce
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(header)
    exclusive_file(destination)
    with archive.open("rb") as source, destination.open("wb") as target:
        target.write(header)
        for block in iter(lambda: source.read(CHUNK), b""):
            target.write(encryptor.update(block))
        target.write(encryptor.finalize()); target.write(encryptor.tag)
        target.flush(); os.fsync(target.fileno())


def decrypt_archive(source, destination, key, max_bytes):
    if source.is_symlink() or not source.is_file():
        raise ValueError("Encrypted input must be a regular file")
    size = source.stat().st_size
    header_length = len(MAGIC) + 12
    if size < header_length + 16 or size > max_bytes + header_length + 16:
        raise ValueError("Invalid encrypted archive size")
    with source.open("rb") as incoming:
        header = incoming.read(header_length)
        if not header.startswith(MAGIC):
            raise ValueError("Unknown encrypted archive format")
        incoming.seek(-16, os.SEEK_END); tag = incoming.read(16); incoming.seek(header_length)
        decryptor = Cipher(algorithms.AES(key), modes.GCM(header[-12:], tag)).decryptor()
        decryptor.authenticate_additional_data(header)
        exclusive_file(destination)
        with destination.open("wb") as output:
            remaining = size - header_length - 16
            while remaining:
                block = incoming.read(min(CHUNK, remaining))
                if not block:
                    raise ValueError("Truncated ciphertext")
                output.write(decryptor.update(block)); remaining -= len(block)
            output.write(decryptor.finalize())


def pull(host, identity, backup, destination, key_file, *, max_bytes=DEFAULT_MAX_BYTES, timeout_seconds=600, sudo=False, downloader=download_archive):
    destination, key_file = private_parent(destination), private_parent(key_file)
    if destination.parent == key_file.parent:
        raise ValueError("Keep the key in a separate private directory")
    command = ssh_command(host, identity, backup, max_bytes, sudo)
    key_created = published = False
    try:
        with tempfile.TemporaryDirectory(dir=destination.parent, prefix=".verify-pull-") as work:
            work = Path(work)
            archive = work / "verified.tar"
            downloader(command, archive, max_bytes, timeout_seconds)
            manifest = unpack_verified(archive, work / "checked", max_bytes)
            key = os.urandom(32)
            exclusive_file(key_file, key); key_created = True
            encrypted = work / "encrypted.tmp"
            encrypt_archive(archive, encrypted, key)
            # Atomic no-clobber publication, even if a destination appeared
            # after initial checks. Never overwrite existing ciphertext.
            os.link(encrypted, destination)
            published = True
            return {"files": len(manifest["files"]), "databases": len(manifest["databases"]), "encryptedBytes": destination.stat().st_size}
    except BaseException:
        if key_created and not published:
            key_file.unlink()
        raise


def recover(source, key_file, destination=None, *, max_bytes=DEFAULT_MAX_BYTES):
    source = Path(source).expanduser().absolute()
    key = read_key(key_file)
    target = private_parent(destination) if destination is not None else None
    # For verification-only runs, use a new private directory in the current
    # user's temp area; plaintext never survives this context on normal exit.
    with tempfile.TemporaryDirectory(dir=target.parent if target else None, prefix="joyniu-restore-check-") as temporary:
        temporary = Path(temporary).resolve()
        archive = temporary / "decrypted.tar"
        decrypt_archive(source, archive, key, max_bytes)
        checked = temporary / "checked"
        manifest = unpack_verified(archive, checked, max_bytes)
        if target:
            # Reserve an empty isolated destination atomically before copying;
            # no existing directory, even empty, is overwritten.
            target.mkdir(mode=0o700, exist_ok=False)
            try:
                for item in (checked / "snapshot").iterdir():
                    shutil.move(str(item), target / item.name)
                for name, counts in manifest["databases"].items():
                    if snapshot._check_db(target / name) != counts:
                        raise ValueError("Restored SQLite verification failed")
            except BaseException:
                shutil.rmtree(target)
                raise
        return {"verified": True, "restored": target is not None, "files": len(manifest["files"]), "databases": len(manifest["databases"])}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0].startswith("--") and argv[0] != "--help":
        argv.insert(0, "pull")
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    grab = commands.add_parser("pull")
    for flag in ("host", "identity", "backup", "destination", "key-file"):
        grab.add_argument("--" + flag, required=True)
    grab.add_argument("--sudo", action="store_true", help="Explicitly read a root-owned backup using remote sudo -n")
    grab.add_argument("--timeout-seconds", type=int, default=600)
    for operation in (grab, commands.add_parser("verify"), commands.add_parser("restore")):
        operation.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
        if operation is not grab:
            operation.add_argument("--source", required=True)
            operation.add_argument("--key-file", required=True)
            if operation.prog.endswith("restore"):
                operation.add_argument("--destination", required=True)
    args = parser.parse_args(argv)
    try:
        if not 1024 <= args.max_bytes <= 64 * 1024**3:
            raise ValueError("Invalid maximum archive size")
        if args.command == "pull":
            if not 1 <= args.timeout_seconds <= 7200:
                raise ValueError("Invalid transfer timeout")
            result = pull(args.host, args.identity, args.backup, args.destination, args.key_file,
                          max_bytes=args.max_bytes, timeout_seconds=args.timeout_seconds, sudo=args.sudo)
        else:
            result = recover(args.source, args.key_file, getattr(args, "destination", None), max_bytes=args.max_bytes)
        print(json.dumps({"ok": True, **result}))
    except Exception:
        print(json.dumps({"ok": False, "message": "Encrypted backup verification failed. Check protected paths, SSH access, key and size limits; no secret details were logged."}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
