"""Local-only encrypted offsite pull/recovery fixtures; never invokes SSH."""
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile

import pytest
from cryptography.exceptions import InvalidTag

SPEC = importlib.util.spec_from_file_location("encrypted_backup", Path(__file__).resolve().parents[3] / "deploy/pull_encrypted_backup.py")
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)


@pytest.fixture
def fixture(tmp_path):
    tmp_path = tmp_path.resolve()
    root = tmp_path / "deployment"
    (root / "data").mkdir(parents=True)
    (root / "deploy/.secrets").mkdir(parents=True)
    (root / "deploy/.secrets/fake.txt").write_text("TEST ONLY FAKE SECRET")
    with sqlite3.connect(root / "data/state.sqlite3") as db:
        db.execute("CREATE TABLE ledger(id TEXT, credits INTEGER)")
        db.execute("INSERT INTO ledger VALUES ('fixture',1000)")
    backup = tmp_path / "backup"
    tool.snapshot.backup(root, backup, quiescent=True)
    archive = tmp_path / "input.tar"
    with tarfile.open(archive, "w") as tar:
        for path in [backup / "manifest.json", backup / "snapshot", *sorted((backup / "snapshot").rglob("*"))]:
            tar.add(path, arcname=path.relative_to(backup).as_posix(), recursive=False)
    identity = tmp_path / "fake-ssh-key"
    identity.write_text("NOT AN SSH KEY; only argv tested")
    enc, key = tmp_path / "encrypted/private.enc", tmp_path / "keys/private.key"
    return backup, archive, identity, enc, key


def fake_download(archive):
    def copy(command, target, maximum, timeout):
        assert command[0] == "ssh" and "StrictHostKeyChecking=yes" in command
        shutil.copyfile(archive, target)
        os.chmod(target, 0o600)
    return copy


def create(fixture):
    backup, archive, identity, enc, key = fixture
    result = tool.pull("ubuntu@example.test", identity, str(backup), enc, key, downloader=fake_download(archive))
    return result, enc, key


def test_encrypted_roundtrip_verifies_every_hash_and_sqlite_then_restores_new_tree(fixture, tmp_path):
    result, enc, key = create(fixture)
    assert result["databases"] == 1 and result["files"] == 2
    assert b"TEST ONLY FAKE SECRET" not in enc.read_bytes()
    assert len(key.read_bytes()) == 32
    assert stat.S_IMODE(enc.stat().st_mode) == stat.S_IMODE(key.stat().st_mode) == 0o600
    assert stat.S_IMODE(enc.parent.stat().st_mode) == stat.S_IMODE(key.parent.stat().st_mode) == 0o700
    assert tool.recover(enc, key)["verified"] is True
    destination = tmp_path / "restores/isolated"
    restored = tool.recover(enc, key, destination)
    assert restored["restored"] is True
    assert (destination / "deploy/.secrets/fake.txt").read_text() == "TEST ONLY FAKE SECRET"
    with sqlite3.connect(destination / "data/state.sqlite3") as db:
        assert db.execute("SELECT credits FROM ledger").fetchone()[0] == 1000
    assert not list(enc.parent.glob(".verify-pull-*"))
    with pytest.raises(ValueError):
        tool.recover(enc, key, destination)


def test_remote_program_is_locally_executable_and_validates_before_emitting_tar(fixture):
    backup, _, _, _, _ = fixture
    # Execute exact transmitted program locally; no SSH and no production data.
    output = subprocess.run([sys.executable, "-", str(backup), str(tool.DEFAULT_MAX_BYTES)], input=tool.remote_script().encode(), capture_output=True, timeout=10)
    assert output.returncode == 0, output.stderr.decode()
    assert {m.name for m in tarfile.open(fileobj=io.BytesIO(output.stdout))} >= {"manifest.json", "snapshot/data/state.sqlite3"}
    (backup / "snapshot/deploy/.secrets/fake.txt").write_text("tampered")
    failed = subprocess.run([sys.executable, "-", str(backup), str(tool.DEFAULT_MAX_BYTES)], input=tool.remote_script().encode(), capture_output=True, timeout=10)
    assert failed.returncode != 0 and failed.stdout == b""
    assert b"tampered" not in failed.stderr


@pytest.mark.parametrize("position", [0, len(tool.MAGIC) + 3, len(tool.MAGIC) + 30, -1])
def test_ciphertext_header_body_nonce_and_tag_tampering_never_publish_restore(fixture, tmp_path, position):
    _, enc, key = create(fixture)
    value = bytearray(enc.read_bytes()); value[position] ^= 1; enc.write_bytes(value)
    target = tmp_path / "restores/never"
    with pytest.raises((ValueError, InvalidTag)):
        tool.recover(enc, key, target)
    assert not target.exists()
    assert not list(target.parent.glob("joyniu-restore-check-*"))


def test_wrong_key_and_insecure_key_permissions_are_rejected(fixture):
    _, enc, key = create(fixture)
    key.write_bytes(os.urandom(32))
    with pytest.raises(InvalidTag):
        tool.recover(enc, key)
    key.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        tool.recover(enc, key)


def test_never_overwrites_artifact_key_or_existing_restore(fixture):
    backup, archive, identity, enc, key = fixture
    enc.parent.mkdir(mode=0o700); enc.write_text("existing")
    with pytest.raises(ValueError):
        create(fixture)
    assert enc.read_text() == "existing" and not key.exists()
    enc.unlink(); key.parent.mkdir(mode=0o700); key.write_text("existing-key")
    with pytest.raises(ValueError):
        create(fixture)
    assert key.read_text() == "existing-key" and not enc.exists()
    with pytest.raises(ValueError, match="separate"):
        tool.pull("ubuntu@example.test", identity, str(backup), enc, enc.parent / "same-dir.key", downloader=fake_download(archive))


@pytest.mark.parametrize("name,kind", [("../escape", "file"), ("/absolute", "file"), ("snapshot/link", "symlink"), ("snapshot/hardlink", "hardlink"), ("snapshot/fifo", "fifo"), ("snapshot/x/../escape", "file"), ("snapshot/duplicate", "duplicate")])
def test_malicious_tar_paths_links_special_files_and_duplicates_are_rejected(fixture, tmp_path, name, kind):
    _, archive, _, enc, key = fixture
    with tarfile.open(archive, "w") as output:
        member = tarfile.TarInfo(name)
        if kind == "symlink": member.type, member.linkname = tarfile.SYMTYPE, "/outside"
        if kind == "hardlink": member.type, member.linkname = tarfile.LNKTYPE, "manifest.json"
        if kind == "fifo": member.type = tarfile.FIFOTYPE
        output.addfile(member)
        if kind == "duplicate": output.addfile(member)
    with pytest.raises(ValueError):
        create(fixture)
    assert not enc.exists() and not key.exists() and not (tmp_path / "escape").exists()


@pytest.mark.parametrize("change", ["omit-database", "extra-directory", "wrong-hash", "wrong-count", "symlink", "journal"])
def test_complete_manifest_directory_and_database_verification(fixture, change):
    backup, _, _, _, _ = fixture
    manifest_path = backup / "manifest.json"
    value = json.loads(manifest_path.read_text())
    if change == "omit-database": value["databases"] = {}
    if change == "extra-directory": (backup / "snapshot/unlisted").mkdir()
    if change == "wrong-hash": value["files"]["deploy/.secrets/fake.txt"]["sha256"] = "0" * 64
    if change == "wrong-count": value["databases"]["data/state.sqlite3"]["ledger"] = 999
    if change == "symlink": (backup / "snapshot/link").symlink_to("/etc/passwd")
    if change == "journal": (backup / "snapshot/data/state.sqlite3-wal").write_bytes(b"fake")
    manifest_path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        tool.strict_verify(backup, tool.DEFAULT_MAX_BYTES)


def test_download_limit_and_timeout_cover_stream_reads_without_real_ssh(tmp_path):
    target = tmp_path / "too-large.tar"
    with pytest.raises(ValueError, match="size limit"):
        tool.download_archive([sys.executable, "-c", "import sys;sys.stdout.buffer.write(b'x'*8192)"], target, 1024, 5)
    with pytest.raises(TimeoutError):
        tool.download_archive([sys.executable, "-c", "import time;time.sleep(3)"], tmp_path / "timeout.tar", 1024, .05)


def test_ssh_argv_quotes_remote_path_and_never_uses_local_shell(fixture):
    _, _, identity, _, _ = fixture
    backup = "/opt/backups/receipt'$(touch /tmp/never); space"
    command = tool.ssh_command("ubuntu@example.test", identity, backup, 2048, sudo=True)
    assert shlex.split(command[-1]) == ["sudo", "-n", "python3", "-", backup, "2048"]
    assert command[-3:-1] == ["--", "ubuntu@example.test"]
    for host in ("-oProxyCommand=bad", "a;cmd", "user@host $(cmd)"):
        with pytest.raises(ValueError):
            tool.ssh_command(host, identity, backup, 2048)


def test_scheduled_marker_is_validated_then_omitted_from_stream(fixture):
    backup, _, _, _, _ = fixture
    scheduled = backup.with_name("scheduled-20260912T110747Z-afb190f6")
    backup.rename(scheduled)
    marker_path = scheduled / "managed-backup.json"
    marker = {"format": "joyniu-operations-v1", "deployment": "/opt/test-deployment",
        "name": scheduled.name, "createdAt": "2026-09-12T11:07:47Z", "manifestSha256": tool.snapshot._digest(scheduled / "manifest.json")}
    marker_path.write_text(json.dumps(marker))
    result = subprocess.run([sys.executable, "-", str(scheduled), str(tool.DEFAULT_MAX_BYTES)], input=tool.remote_script().encode(), capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr.decode()
    assert "managed-backup.json" not in {member.name for member in tarfile.open(fileobj=io.BytesIO(result.stdout))}
    marker["manifestSha256"] = "0" * 64
    marker_path.write_text(json.dumps(marker))
    with pytest.raises(ValueError, match="marker"):
        tool.strict_verify(scheduled, tool.DEFAULT_MAX_BYTES)


def test_sqlite_checkpoint_scope_matches_producer_and_opaque_runtime_files_keep_hash_checks(fixture):
    backup, _, _, _, _ = fixture
    runtime = backup / "snapshot/deploy/.secrets/runtime.sqlite3"
    with sqlite3.connect(runtime) as db:
        db.execute("CREATE TABLE runtime(id INTEGER)")
    manifest_path = backup / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"]["deploy/.secrets/runtime.sqlite3"] = {"sha256": tool.snapshot._digest(runtime), "size": runtime.stat().st_size}
    manifest_path.write_text(json.dumps(manifest))
    assert len(tool.strict_verify(backup, tool.DEFAULT_MAX_BYTES)["databases"]) == 1
    runtime.write_bytes(b"changed")
    with pytest.raises(ValueError):
        tool.strict_verify(backup, tool.DEFAULT_MAX_BYTES)
