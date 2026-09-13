from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import sqlite3
from threading import Barrier

import pytest

from app.account_workspace import (
    AccountWorkspaceStore,
    WorkspaceConflictError,
    WorkspaceTooLargeError,
    WorkspaceValidationError,
)


def workspace(name="账号 A 的项目"):
    snapshot = {
        "model": {"kind": "feature_model", "features": [{"id": "hole", "radius": 3.25}], "token": "CAD parameter"},
        "messages": [{"id": "m1", "role": "ai", "text": "孔径 6.5 mm", "usage": {"input_tokens": 120}}],
        "generation": {"artifacts": [{"url": "/api/v1/cad/files/part.step", "token": "artifact-scoped-download"}]},
        "drawingJob": {"cadTask": {"runId": "run_saved", "status": "completed"}},
    }
    return {
        "schemaVersion": 1, "activeProjectId": "p1", "activeFileId": "f1", "futureField": {"enabled": True},
        "projects": [{"id": "p1", "name": name, "files": [{
            "id": "f1", "name": "安装支架", "snapshot": snapshot,
            "versions": [{"id": "v1", "number": 1, "note": "保留历史", "snapshot": deepcopy(snapshot)}],
        }]}],
        "trash": [{"type": "file", "file": {"id": "deleted", "snapshot": {"model": None}}}],
    }


def test_snapshot_round_trip_preserves_all_json_and_survives_restart(tmp_path):
    database = tmp_path / "workspaces.sqlite3"
    original = workspace()
    store = AccountWorkspaceStore(database)
    assert store.load("A") == {"revision": 0, "snapshot": None, "updatedAt": None}
    saved = store.save("A", original, expected_revision=0)
    assert saved["snapshot"] == original
    assert saved["revision"] == 1
    assert saved["updatedAt"].endswith("Z")
    original["projects"][0]["name"] = "outside mutation"
    assert store.load("A")["snapshot"]["projects"][0]["name"] == "账号 A 的项目"
    store.close()
    reopened = AccountWorkspaceStore(database)
    try:
        assert reopened.load("A") == saved
        assert reopened.load("B") == {"revision": 0, "snapshot": None, "updatedAt": None}
    finally:
        reopened.close()


def test_each_account_has_its_own_revision_and_conflict_does_not_write_audit(tmp_path):
    database = tmp_path / "accounts.sqlite3"
    store = AccountWorkspaceStore(database)
    try:
        first = store.save("A", workspace(), expected_revision=0)
        second = store.save("A", workspace("更新"), expected_revision=1)
        with pytest.raises(WorkspaceConflictError) as conflict:
            store.save("A", workspace("过期"), expected_revision=0)
        assert conflict.value.revision == 2
        assert store.save("B", workspace("账号 B"), expected_revision=0)["revision"] == 1
        assert store.load("A") == second
        assert second["updatedAt"] >= first["updatedAt"]
        with sqlite3.connect(database) as connection:
            rows = connection.execute("SELECT owner_id, revision, action, created_at FROM account_workspace_audit ORDER BY owner_id, revision").fetchall()
            assert [(r[0], r[1], r[2]) for r in rows] == [("A", 1, "workspace.saved"), ("A", 2, "workspace.saved"), ("B", 1, "workspace.saved")]
            assert rows[1][3] == second["updatedAt"]
    finally:
        store.close()


def test_two_independent_connections_cannot_overwrite_same_revision(tmp_path):
    database = tmp_path / "concurrent.sqlite3"
    stores = [AccountWorkspaceStore(database), AccountWorkspaceStore(database)]
    barrier = Barrier(2)

    def save(index):
        barrier.wait()
        try:
            return stores[index].save("A", workspace(f"writer-{index}"), expected_revision=0)
        except WorkspaceConflictError as exc:
            return exc

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(save, [0, 1]))
        successes = [value for value in results if isinstance(value, dict)]
        conflicts = [value for value in results if isinstance(value, WorkspaceConflictError)]
        assert len(successes) == len(conflicts) == 1
        assert conflicts[0].revision == 1
        assert stores[1].load("A") == successes[0]
    finally:
        for store in stores:
            store.close()


@pytest.mark.parametrize("revision", [None, True, -1, 0.5, "0", 2**53])
def test_revision_is_required_and_is_an_integer(revision):
    store = AccountWorkspaceStore()
    try:
        with pytest.raises(WorkspaceValidationError):
            store.save("A", workspace(), expected_revision=revision)
        assert store.load("A")["revision"] == 0
    finally:
        store.close()


@pytest.mark.parametrize("key", ["access_token", "accessToken", "refreshToken", "session_token", "authorization", "password"])
def test_nested_login_credentials_are_rejected_without_mutation(key):
    store = AccountWorkspaceStore()
    try:
        payload = workspace()
        payload["projects"][0]["files"][0]["versions"][0]["snapshot"][key] = "must-not-persist"
        with pytest.raises(WorkspaceValidationError, match="登录凭据"):
            store.save("A", payload, expected_revision=0)
        assert store.load("A")["snapshot"] is None
    finally:
        store.close()


def test_size_limit_counts_utf8_and_rejects_non_json_data():
    store = AccountWorkspaceStore(max_bytes=200)
    try:
        with pytest.raises(WorkspaceTooLargeError):
            store.save("A", {"schemaVersion": 1, "projects": [], "text": "中" * 60}, expected_revision=0)
        with pytest.raises(WorkspaceValidationError):
            store.save("A", {"schemaVersion": 1, "projects": [], "value": float("nan")}, expected_revision=0)
        assert store.load("A")["revision"] == 0
    finally:
        store.close()
