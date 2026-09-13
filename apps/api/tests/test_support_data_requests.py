import hashlib
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.platform import AuthService
from app.support_api import create_support_router


@pytest.fixture
def context(tmp_path):
    auth = AuthService(tmp_path / "account.sqlite3", token_secret="data-request-local-test-" * 3)
    users = {name: auth.create_user(f"{name}@example.test", "local-password-123", name, roles=[role]) for name, role in (("customer", "viewer"), ("other", "viewer"), ("support", "support"), ("admin", "admin"))}
    source = tmp_path / "customer.dxf"
    source.write_bytes(b"existing customer data remains unchanged")
    router = create_support_router(SimpleNamespace(auth=auth), cad_store=SimpleNamespace(load=lambda _: {"owner": users["customer"].id}))
    app = FastAPI(); app.include_router(router, prefix="/api/v1")
    headers = lambda name: {"Authorization": "Bearer " + auth.issue_token(users[name]).token}
    with TestClient(app) as client:
        yield client, headers, source, router.support_service
    auth.close()


def request(**overrides):
    return {"subject": "删除指定图纸和模型", "category": "data_deletion", "body": "该项目已结束，请先核对保留范围。", "runId": "my-run", "drawingConsent": False,
            "dataRequest": {"scopes": ["source_files", "models", "backups"], "scopeDescription": "my-run 及其导出结果，备份请告知期限", "acknowledged": True}, "idempotencyKey": "create-request", **overrides}


def receipt(revision=1, **overrides):
    return {"revision": revision, "outcome": "not_processed", "handledScope": "已完成范围核对，本次未执行删除", "retainedScope": "原图、模型和备份均保留，等待客户确认", "retentionPlan": "具体保留期限尚待双方确认，不承诺已删除备份", "evidenceRef": "LOCAL-REVIEW-001", "confirmed": True, "idempotencyKey": "receipt-one", **overrides}


def test_customer_request_and_manual_receipt_are_owned_persistent_and_non_destructive(context):
    client, headers, source, service = context
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    path = "/api/v1/support/tickets"
    response = client.post(path, json=request(), headers=headers("customer"))
    assert response.status_code == 201
    ticket = response.json()
    assert ticket["dataRequest"]["scopes"] == ["backups", "models", "source_files"]
    assert ticket["dataReceipts"] == [] and ticket["drawingConsent"] is False
    assert client.post(path, json=request(), headers=headers("customer")).json()["id"] == ticket["id"]
    assert client.get(path + "/" + ticket["id"], headers=headers("other")).status_code == 404
    admin = "/api/v1/support/admin/tickets/" + ticket["id"]
    endpoint = admin + "/data-receipts"
    assert client.post(endpoint, json=receipt(), headers=headers("customer")).status_code == 403
    assert client.post(endpoint, json=receipt(), headers=headers("other")).status_code == 403
    result = client.post(endpoint, json=receipt(), headers=headers("support"))
    assert result.status_code == 201 and result.headers["cache-control"] == "no-store"
    assert result.json()["status"] == "resolved"
    assert client.post(endpoint, json=receipt(), headers=headers("support")).json()["revision"] == 2
    own = client.get(path + "/" + ticket["id"], headers=headers("customer")).json()
    assert len(own["dataReceipts"]) == 1
    assert own["dataReceipts"][0]["outcome"] == "not_processed"
    assert own["dataReceipts"][0]["evidenceRef"] == "LOCAL-REVIEW-001"
    assert own["dataRequest"]["scopeDescription"].startswith("my-run")
    assert not {"assignedTo", "internalNoteCount"}.intersection(own)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert service._db.execute("SELECT count(*) FROM support_data_receipts").fetchone()[0] == 1
    assert "data.receipt.created" in [item["action"] for item in client.get(admin + "/audit", headers=headers("admin")).json()["items"]]
    for table in ("support_data_requests", "support_data_receipts"):
        with pytest.raises(Exception, match="immutable support record"):
            service._db.execute(f"DELETE FROM {table}")


def test_scope_confirmation_and_outcome_evidence_are_required_and_versioned(context):
    client, headers, _, _ = context
    path = "/api/v1/support/tickets"
    for data in (None, {"scopes": [], "scopeDescription": "specific", "acknowledged": True}, {"scopes": ["models"], "scopeDescription": "specific", "acknowledged": False}, {"scopes": ["models", "models"], "scopeDescription": "specific", "acknowledged": True}):
        assert client.post(path, json=request(dataRequest=data), headers=headers("customer")).status_code == 422
    assert client.get(path, headers=headers("customer")).json()["total"] == 0
    ticket = client.post(path, json=request(), headers=headers("customer")).json()
    admin = "/api/v1/support/admin/tickets/" + ticket["id"]
    assert client.patch(admin, json={"revision": 1, "status": "resolved", "reason": "只改变状态不能认定删除", "idempotencyKey": "resolve"}, headers=headers("support")).status_code == 422
    for patch in ({"confirmed": False}, {"evidenceRef": ""}, {"retentionPlan": ""}, {"outcome": "deleted_everything"}, {"revision": 0}):
        assert client.post(admin + "/data-receipts", json=receipt(**patch), headers=headers("support")).status_code == 422
    first = client.post(admin + "/data-receipts", json=receipt(outcome="pending_confirmation"), headers=headers("support")).json()
    assert first["status"] == "waiting_customer" and first["revision"] == 2
    assert client.post(admin + "/data-receipts", json=receipt(idempotencyKey="stale"), headers=headers("support")).status_code == 409
    assert client.post(admin + "/data-receipts", json=receipt(2, idempotencyKey="receipt-one"), headers=headers("support")).status_code == 409
    result = client.post(admin + "/data-receipts", json=receipt(2, idempotencyKey="receipt-two"), headers=headers("support")).json()
    assert len(result["dataReceipts"]) == 2
    assert result["dataRequest"] == ticket["dataRequest"]
