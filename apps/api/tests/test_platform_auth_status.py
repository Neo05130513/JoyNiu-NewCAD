"""Public initialization status and production account-creation boundaries."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.platform_api import build_platform_services, create_platform_router


@pytest.fixture
def account_api():
    services = build_platform_services(":memory:", auth_secret="auth-status-test-secret-32-bytes-long")
    app = FastAPI()
    app.include_router(create_platform_router(services), prefix="/api/v1")
    with TestClient(app) as client:
        yield services, client
    services.close()


def account(email="first@example.test", roles=None):
    return {"email": email, "password": "test-password-long-enough", "displayName": "Test user", "roles": roles or ["admin"]}


@pytest.mark.parametrize("environment", ["production", "prod", " Production "])
def test_production_empty_store_reports_no_bootstrap_and_rejects_registration(account_api, monkeypatch, environment):
    services, client = account_api
    monkeypatch.setenv("JOYNIU_ENV", environment)
    response = client.get("/api/v1/auth/status")
    assert response.status_code == 200
    assert response.json() == {"initialized": False, "bootstrapAllowed": False}
    assert response.headers["cache-control"] == "no-store"
    assert services.auth.count_users() == 0
    assert client.post("/api/v1/auth/users", json=account()).status_code == 403
    assert services.auth.count_users() == 0
    assert client.get("/api/v1/auth/status").json() == response.json()


@pytest.mark.parametrize("environment", [None, "development", "local", "test"])
def test_local_empty_store_can_initialize_once_and_then_log_in(account_api, monkeypatch, environment):
    services, client = account_api
    if environment is None:
        monkeypatch.delenv("JOYNIU_ENV", raising=False)
    else:
        monkeypatch.setenv("JOYNIU_ENV", environment)
    assert client.get("/api/v1/auth/status").json() == {"initialized": False, "bootstrapAllowed": True}
    assert services.auth.count_users() == 0
    created = client.post("/api/v1/auth/users", json=account())
    assert created.status_code == 201
    assert client.get("/api/v1/auth/status").json() == {"initialized": True, "bootstrapAllowed": False}
    assert client.post("/api/v1/auth/users", json=account("second@example.test")).status_code == 401
    login = client.post("/api/v1/auth/login", json={"email": account()["email"], "password": account()["password"]})
    assert login.status_code == 200
    identity = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {login.json()['access_token']}"})
    assert identity.status_code == 200
    assert identity.json()["id"] == created.json()["id"]


def test_production_existing_admin_can_create_users_but_anonymous_and_designer_cannot(account_api, monkeypatch):
    services, client = account_api
    monkeypatch.setenv("JOYNIU_ENV", "production")
    admin = services.auth.create_user("private-admin@example.test", "test-password-long-enough", "Private admin", roles=["admin"])
    designer = services.auth.create_user("private-designer@example.test", "test-password-long-enough", "Private designer", roles=["designer"])
    before = services.auth.count_users()
    response = client.get("/api/v1/auth/status")
    assert response.status_code == 200
    # Exact keys assert that public state includes no email, id, role or count.
    assert response.json() == {"initialized": True, "bootstrapAllowed": False}
    assert services.auth.count_users() == before
    payload = account("new-member@example.test", ["viewer"])
    assert client.post("/api/v1/auth/users", json=payload).status_code == 401
    designer_auth = {"Authorization": f"Bearer {services.auth.issue_token(designer).token}"}
    assert client.post("/api/v1/auth/users", json=payload, headers=designer_auth).status_code == 403
    assert services.auth.count_users() == before
    admin_auth = {"Authorization": f"Bearer {services.auth.issue_token(admin).token}"}
    created = client.post("/api/v1/auth/users", json=payload, headers=admin_auth)
    assert created.status_code == 201
    assert created.json()["roles"] == ["viewer"]
    assert services.auth.count_users() == before + 1
