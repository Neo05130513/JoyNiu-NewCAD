import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth_account_api import create_account_router
from app.platform import AuthService, AuthenticationError, AuthorizationError, ConflictError, ValidationError
from app.platform_api import build_platform_services, create_platform_router

PASSWORD = "alias-regression-password"


@pytest.fixture
def services(monkeypatch):
    monkeypatch.setattr(AuthService, "_PBKDF2_ITERATIONS", 1000)
    monkeypatch.setenv("JOYNIU_ENV", "production")
    value = build_platform_services(":memory:", auth_secret="alias-test-secret-with-at-least-32-bytes")
    value.auth.create_user("owner@example.test", PASSWORD, "Owner", roles=("admin",), user_id="owner")
    value.auth.create_user("customer@example.test", PASSWORD, "Customer", roles=("designer",), user_id="customer")
    yield value
    value.close()


def test_alias_real_login_keeps_email_identity_roles_and_revocation(services):
    auth = services.auth
    old = auth.authenticate("owner@example.test", PASSWORD)
    auth.set_login_alias("owner", " Admin ", actor_id="owner")
    with pytest.raises(AuthenticationError):
        auth.verify_token(old.token)
    app = FastAPI()
    app.include_router(create_platform_router(services), prefix="/api/v1")
    app.include_router(create_account_router(services), prefix="/api/v1")
    with TestClient(app, base_url="https://cad.example.test") as client:
        for identifier in ["admin", "ADMIN", "owner@example.test"]:
            result = client.post("/api/v1/auth/login", json={"email": identifier, "password": PASSWORD})
            assert result.status_code == 200
            user = auth.verify_token(result.json()["access_token"])
            assert (user.id, user.email, user.roles) == ("owner", "owner@example.test", ("admin",))
        for identifier in ["missing", "not a valid alias", "@invalid"]:
            assert client.post("/api/v1/auth/login", json={"email": identifier, "password": PASSWORD}).status_code == 401
        assert client.post("/api/v1/auth/login", json={"email": "admin", "password": "incorrect"}).status_code == 401
    assert auth.count_users() == 2
    row = auth._connection.execute("SELECT details_json FROM auth_audit WHERE action='user.login_alias_changed'").fetchone()
    assert json.loads(row[0]) == {"alias": "admin", "previousAlias": None}
    assert PASSWORD not in row[0]


def test_unique_alias_rename_disabled_user_and_no_privilege_change(services):
    auth = services.auth
    auth.set_login_alias("customer", "customer-login", actor_id="owner")
    token = auth.authenticate("customer-login", PASSWORD)
    assert token.user.roles == ("designer",)
    with pytest.raises(ConflictError):
        auth.set_login_alias("owner", "CUSTOMER-LOGIN", actor_id="owner")
    assert auth.authenticate("customer-login", PASSWORD).user.id == "customer"
    auth.set_login_alias("customer", "new-login", actor_id="owner")
    with pytest.raises(AuthenticationError):
        auth.authenticate("customer-login", PASSWORD)
    with pytest.raises(AuthenticationError):
        auth.verify_token(token.token)
    auth.set_active("customer", False, actor_id="owner")
    with pytest.raises(AuthenticationError):
        auth.authenticate("new-login", PASSWORD)


def test_alias_assignment_rechecks_operator_permission(services):
    auth = services.auth
    with pytest.raises(AuthorizationError):
        auth.set_login_alias("customer", "not-allowed", actor_id="customer")
    auth.create_user("second@example.test", PASSWORD, "Second", roles=("admin",), user_id="second")
    auth.assign_roles("owner", ("viewer",), actor_id="second")
    with pytest.raises(AuthorizationError):
        auth.set_login_alias("customer", "still-not-allowed", actor_id="owner")
    auth.set_active("second", True, actor_id="second")
    assert auth._connection.execute("SELECT count(*) FROM auth_login_aliases").fetchone()[0] == 0


@pytest.mark.parametrize("alias", ["ab", "1admin", "admin@example.test", "a" * 33, "客户", "admin login"])
def test_alias_validation_does_not_relax_registration_or_password_rules(services, alias):
    auth = services.auth
    with pytest.raises(ValidationError):
        auth.set_login_alias("owner", alias, actor_id="owner")
    with pytest.raises(ValidationError):
        auth.create_user("admin", PASSWORD, "Invalid email")
    with pytest.raises(ValidationError):
        auth.reset_password("owner", "short", actor_id="owner")
