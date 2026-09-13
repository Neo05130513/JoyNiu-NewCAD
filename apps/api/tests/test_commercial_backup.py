"""Offline backup/restore drill with fake secret files and uncheckpointed WAL."""
import importlib.util
from pathlib import Path
import sqlite3
import stat

import pytest


SPEC = importlib.util.spec_from_file_location("commercial_backup", Path(__file__).resolve().parents[3] / "deploy" / "commercial_backup.py")
backup_tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backup_tool)


@pytest.fixture
def deployment(tmp_path):
    root = tmp_path / "deployment"
    (root / "data/cad-agent").mkdir(parents=True)
    (root / "deploy/.secrets/codex").mkdir(parents=True)
    (root / "deploy/.env.production").write_text("FAKE_ONLY=not-real\n")
    (root / "deploy/.secrets/codex/auth.json").write_text('{"fake":"test-only"}')
    (root / "data/cad-agent/original.dwg").write_bytes(b"fixture-source-file")
    (root / "data/backups").mkdir()
    (root / "data/backups/customer-file.txt").write_text("must not exclude customer data")
    (root / "data/empty-workspace").mkdir()
    (root / "compose.yaml").write_text("services: {}")
    (root / ".env").write_text("COMPOSE_FILE=compose.yaml:deploy/compose.codex.yaml")
    connections = []
    for relative, tables in [("data/joyniu.sqlite3", ["users", "auth_sessions", "account_workspaces", "billing_ledger", "billing_refund_events", "billing_policy_versions"]),
                             ("data/cad-agent/runs.sqlite3", ["cad_runs", "cad_provider_calls", "cad_job_attempts"])]:
        db = sqlite3.connect(root / relative)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA wal_autocheckpoint=0")
        for table in tables:
            db.execute(f"CREATE TABLE {table}(id TEXT PRIMARY KEY, payload TEXT)")
            db.execute(f"INSERT INTO {table} VALUES ('fixture','must survive WAL')")
        db.commit()
        connections.append(db)
    yield root
    for db in connections: db.close()


def test_full_two_database_restore_survives_wal_and_preserves_cloud_wallet_and_sources(deployment, tmp_path):
    snapshot, restored = tmp_path / "private-backup", tmp_path / "restored"
    result = backup_tool.backup(deployment, snapshot, quiescent=True)
    assert result["databases"] == 2
    assert stat.S_IMODE(snapshot.stat().st_mode) == 0o700
    manifest = backup_tool.verify(snapshot)
    assert manifest["databases"]["data/joyniu.sqlite3"]["account_workspaces"] == 1
    assert manifest["databases"]["data/joyniu.sqlite3"]["billing_ledger"] == 1
    assert manifest["databases"]["data/cad-agent/runs.sqlite3"]["cad_provider_calls"] == 1
    backup_tool.restore(snapshot, restored)
    assert (restored / "deploy/.env.production").read_bytes() == (deployment / "deploy/.env.production").read_bytes()
    assert (restored / "data/cad-agent/original.dwg").read_bytes() == b"fixture-source-file"
    assert (restored / "data/backups/customer-file.txt").is_file()
    assert (restored / "data/empty-workspace").is_dir()
    for relative in manifest["databases"]:
        assert backup_tool._check_db(restored / relative) == manifest["databases"][relative]
    assert not list((snapshot / "snapshot").rglob("*-wal"))


def test_running_writers_or_in_tree_destination_are_refused(deployment, tmp_path):
    with pytest.raises(ValueError): backup_tool.backup(deployment, tmp_path / "no-attestation")
    with pytest.raises(ValueError): backup_tool.backup(deployment, deployment / "backup", quiescent=True)


def test_restore_never_overwrites_existing_tree_and_rejects_tampering(deployment, tmp_path):
    snapshot = tmp_path / "backup"
    backup_tool.backup(deployment, snapshot, quiescent=True)
    with pytest.raises(ValueError): backup_tool.restore(snapshot, deployment)
    (snapshot / "snapshot/data/cad-agent/original.dwg").write_bytes(b"changed")
    with pytest.raises(ValueError): backup_tool.restore(snapshot, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


def test_symlink_outside_deployment_is_not_followed(deployment, tmp_path):
    external = tmp_path / "external"
    external.write_text("test-only-not-to-copy")
    (deployment / "deploy/outside").symlink_to(external)
    with pytest.raises(ValueError): backup_tool.backup(deployment, tmp_path / "backup", quiescent=True)
    assert not (tmp_path / "backup/manifest.json").exists()


def test_codex_runtime_wrapper_links_are_precisely_excluded_and_credentials_survive(deployment, tmp_path):
    runtime = deployment / "deploy/.secrets/codex/tmp/arg0/codex-arg0l2OEjo"
    runtime.mkdir(parents=True)
    for name in ("codex-linux-sandbox", "apply_patch", "codex-execve-wrapper", "applypatch"):
        (runtime / name).symlink_to("/opt/codex/bin/codex")
    preserved = {
        "deploy/.secrets/codex/config.toml": "fixture_config = true\n",
        "deploy/.secrets/codex/session-state.json": '{"fixture":"keep"}',
        "deploy/.secrets/egress/config.yaml": "fixture_only: keep\n",
        "deploy/.secrets/other/tmp/required.txt": "other secret tmp must remain",
        "data/tmp/customer.dwg": "customer tmp data must remain",
    }
    for relative, value in preserved.items():
        path = deployment / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
    snapshot, restored = tmp_path / "backup", tmp_path / "restored"
    backup_tool.backup(deployment, snapshot, quiescent=True)
    manifest = backup_tool.verify(snapshot)
    assert manifest["exclusions"] == [{"path": "deploy/.secrets/codex/tmp", "reason": backup_tool.EXCLUDED_DIRECTORIES["deploy/.secrets/codex/tmp"]}]
    assert not any(path.startswith("deploy/.secrets/codex/tmp/") for path in manifest["files"])
    assert not (snapshot / "snapshot/deploy/.secrets/codex/tmp").exists()
    backup_tool.restore(snapshot, restored)
    for relative in ["deploy/.secrets/codex/auth.json", *preserved]:
        assert (restored / relative).read_bytes() == (deployment / relative).read_bytes()
    assert not (restored / "deploy/.secrets/codex/tmp").exists()


@pytest.mark.parametrize("relative", ["deploy/.secrets/codex/auth.json", "deploy/.secrets/codex/config.toml",
    "deploy/.secrets/codex/another/tmp/link", "deploy/.secrets/other/tmp/link", "data/tmp/link", "deploy/.secrets/codex/tmp"])
def test_no_other_secret_or_tmp_symlinks_are_skipped_or_followed(deployment, tmp_path, relative):
    path = deployment / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    path.symlink_to(tmp_path / "missing-sensitive-target")
    with pytest.raises(ValueError, match="Symlinks"):
        backup_tool.backup(deployment, tmp_path / "backup", quiescent=True)
    assert not (tmp_path / "backup/manifest.json").exists()
