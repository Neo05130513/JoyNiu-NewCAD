from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth_account_api import COOKIE_NAME, create_account_router
from app.platform import AuthService, AuthenticationError, AuthorizationError, RateLimitError
from app.platform_api import build_platform_services, create_platform_router

SECRET = "account-regression-secret-at-least-32-bytes"
PASSWORD = "original-password-123"
NEXT_PASSWORD = "new-password-456"
ORIGIN = "https://cad.example.test"
SECURE = {"Origin": ORIGIN, "X-JoyNiu-CSRF": "1"}


@pytest.fixture(autouse=True)
def bounded_password_test_cost(monkeypatch):
    # Exercise the actual hash and verification implementation with a small
    # work factor; the existing platform regressions cover the production one.
    monkeypatch.setattr(AuthService, "_PBKDF2_ITERATIONS", 1000)
    monkeypatch.setenv("JOYNIU_ENV", "production")
    monkeypatch.delenv("JOYNIU_PUBLIC_REGISTRATION", raising=False)
    monkeypatch.delenv("JOYNIU_AUTH_ALLOWED_ORIGINS", raising=False)


@pytest.fixture
def account_api():
    services = build_platform_services(":memory:", auth_secret=SECRET)
    app = FastAPI()
    app.include_router(create_platform_router(services), prefix="/api/v1")
    app.include_router(create_account_router(services), prefix="/api/v1")
    with TestClient(app, base_url=ORIGIN) as client:
        yield services, client
    services.close()


def create_user(auth, email="customer@example.test", roles=("designer",)):
    return auth.create_user(email, PASSWORD, "客户", roles=roles)


def login(client, email="customer@example.test", password=PASSWORD, **kwargs):
    return client.post("/api/v1/auth/login", json={"email": email, "password": password}, headers=SECURE, **kwargs)


def bearer(response):
    return {"Authorization": "Bearer " + response.json()["access_token"]}


def test_registration_switch_is_default_closed_and_capabilities_are_honest(account_api):
    services, client = account_api
    capabilities = client.get("/api/v1/auth/account-capabilities").json()
    assert capabilities["registrationEnabled"] is False
    assert capabilities["passwordResetEmailAvailable"] is False
    assert capabilities["emailVerificationAvailable"] is False
    assert capabilities["persistentSessionAvailable"] is True
    assert client.post("/api/v1/auth/register", json={"email": "a@example.test", "password": PASSWORD, "displayName": "A"}, headers=SECURE).status_code == 403
    assert services.auth.count_users() == 0


@pytest.mark.parametrize("extra", [{"roles": ["admin"]}, {"role": "admin"}, {"permissions": ["*"]}, {"active": True}])
def test_registration_rejects_privilege_fields(account_api, monkeypatch, extra):
    services, client = account_api
    monkeypatch.setenv("JOYNIU_PUBLIC_REGISTRATION", "true")
    response = client.post("/api/v1/auth/register", headers=SECURE,
        json={"email": "a@example.test", "password": PASSWORD, "displayName": "A", **extra})
    assert response.status_code == 422
    assert services.auth.count_users() == 0


def test_registration_grants_designer_only_and_http_only_cookie(account_api, monkeypatch):
    services, client = account_api
    monkeypatch.setenv("JOYNIU_PUBLIC_REGISTRATION", "true")
    response = client.post("/api/v1/auth/register", headers=SECURE,
        json={"email": "Customer@Example.test", "password": PASSWORD, "displayName": "客户"})
    assert response.status_code == 201
    body = response.json()
    assert body["user"]["roles"] == ["designer"]
    assert "*" not in body["user"]["permissions"]
    assert body["user"]["email"] == "customer@example.test"
    assert body["persistent_session"] is True
    cookie = response.headers["set-cookie"]
    for expected in ["HttpOnly", "Secure", "SameSite=strict", "Path=/api/v1/auth"]:
        assert expected in cookie
    raw = client.cookies.get(COOKIE_NAME)
    assert raw and raw not in response.text
    assert services.auth.verify_token(body["access_token"]).id == body["user"]["id"]
    assert "password" not in json.dumps(body["user"])
    assert client.get("/api/v1/auth/users", headers=bearer(response)).status_code == 403


def test_login_compatibility_refresh_rotation_and_replay_revokes_family(account_api):
    services, client = account_api
    create_user(services.auth)
    initial = login(client)
    assert initial.status_code == 200
    for key in ["access_token", "token_type", "expires_at", "user"]:
        assert key in initial.json()
    old_cookie = client.cookies.get(COOKIE_NAME)
    refreshed = client.post("/api/v1/auth/refresh", json={}, headers=SECURE)
    assert refreshed.status_code == 200
    fresh_cookie = client.cookies.get(COOKIE_NAME)
    assert fresh_cookie != old_cookie
    assert refreshed.json()["access_token"] != initial.json()["access_token"]
    assert client.get("/api/v1/auth/me", headers=bearer(initial)).status_code == 200
    replay = client.post("/api/v1/auth/refresh", json={}, headers={**SECURE, "Cookie": f"{COOKIE_NAME}={old_cookie}"})
    assert replay.status_code == 401
    assert client.get("/api/v1/auth/me", headers=bearer(refreshed)).status_code == 401
    assert client.post("/api/v1/auth/refresh", json={}, headers={**SECURE, "Cookie": f"{COOKIE_NAME}={fresh_cookie}"}).status_code == 401


@pytest.mark.parametrize("headers", [{"Origin": ORIGIN}, {"Origin": "https://evil.example", "X-JoyNiu-CSRF": "1"},
    {"Origin": "null", "X-JoyNiu-CSRF": "1"}, {"Sec-Fetch-Site": "cross-site", "X-JoyNiu-CSRF": "1"}])
def test_cookie_csrf_failures_do_not_consume_refresh_token(account_api, headers):
    services, client = account_api
    create_user(services.auth)
    login(client)
    assert client.post("/api/v1/auth/refresh", json={}, headers=headers).status_code == 403
    assert client.post("/api/v1/auth/refresh", json={}, headers=SECURE).status_code == 200


def test_cross_origin_login_and_form_requests_are_rejected(account_api):
    services, client = account_api
    create_user(services.auth)
    assert client.post("/api/v1/auth/login", json={"email": "customer@example.test", "password": PASSWORD},
                       headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post("/api/v1/auth/login", data={"email": "customer@example.test", "password": PASSWORD}).status_code in {415, 422}
    # Existing CLI consumers use JSON without a browser Origin.
    assert client.post("/api/v1/auth/login", json={"email": "customer@example.test", "password": PASSWORD}).status_code == 200


def test_logout_revokes_current_session_and_clears_cookie(account_api):
    services, client = account_api
    user = create_user(services.auth)
    unrelated, other_cookie, _ = services.auth.create_session(user)
    response = login(client)
    raw = client.cookies.get(COOKIE_NAME)
    logged_out = client.post("/api/v1/auth/logout", json={}, headers={**SECURE, **bearer(response)})
    assert logged_out.json() == {"ok": True}
    assert client.cookies.get(COOKIE_NAME) is None
    assert client.get("/api/v1/auth/me", headers=bearer(response)).status_code == 401
    with pytest.raises(AuthenticationError):
        services.auth.refresh_session(raw)
    assert services.auth.verify_token(unrelated.token).id == user.id
    assert services.auth.refresh_session(other_cookie)[0].user.id == user.id
    assert client.post("/api/v1/auth/logout", json={}, headers={**SECURE, **bearer(response)}).status_code == 200


def test_change_password_revokes_all_prior_tokens_but_returns_a_new_session(account_api):
    services, client = account_api
    user = create_user(services.auth)
    legacy = services.auth.issue_token(user)
    other, raw_other, _ = services.auth.create_session(user)
    original = login(client)
    bad = client.post("/api/v1/auth/password/change", json={"currentPassword": "incorrect", "newPassword": NEXT_PASSWORD}, headers={**SECURE, **bearer(original)})
    assert bad.status_code == 401
    assert services.auth.verify_token(original.json()["access_token"]).id == user.id
    changed = client.post("/api/v1/auth/password/change", json={"currentPassword": PASSWORD, "newPassword": NEXT_PASSWORD}, headers={**SECURE, **bearer(original)})
    assert changed.status_code == 200
    for token in [legacy.token, other.token, original.json()["access_token"]]:
        with pytest.raises(AuthenticationError):
            services.auth.verify_token(token)
    with pytest.raises(AuthenticationError):
        services.auth.refresh_session(raw_other)
    assert services.auth.verify_token(changed.json()["access_token"]).id == user.id
    assert services.auth.authenticate(user.email, NEXT_PASSWORD).user.roles == user.roles
    with pytest.raises(AuthenticationError):
        services.auth.authenticate(user.email, PASSWORD)


def test_administrator_reset_is_authorized_revokes_sessions_and_does_not_return_password(account_api):
    services, client = account_api
    user = create_user(services.auth)
    admin = create_user(services.auth, "admin@example.test", ("admin",))
    original = login(client)
    path = f"/api/v1/auth/users/{user.id}/password/reset"
    assert client.post(path, json={"newPassword": NEXT_PASSWORD}, headers={**SECURE, **bearer(original)}).status_code == 403
    admin_token = services.auth.issue_token(admin)
    response = client.post(path, json={"newPassword": NEXT_PASSWORD}, headers={**SECURE, "Authorization": f"Bearer {admin_token.token}"})
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert PASSWORD not in response.text and NEXT_PASSWORD not in response.text
    assert client.get("/api/v1/auth/me", headers=bearer(original)).status_code == 401
    assert services.auth.authenticate(user.email, NEXT_PASSWORD).user.id == user.id
    audit = json.dumps(services.auth.list_audit_events())
    assert PASSWORD not in audit and NEXT_PASSWORD not in audit


def test_forgot_password_is_unavailable_without_email_and_does_not_enumerate_users(account_api):
    services, client = account_api
    create_user(services.auth)
    responses = [client.post("/api/v1/auth/password/forgot", json={"email": email}, headers=SECURE)
                 for email in ["customer@example.test", "absent@example.test"]]
    assert responses[0].status_code == responses[1].status_code == 503
    assert responses[0].json() == responses[1].json()
    assert "管理员" in responses[0].text and "未配置" in responses[0].text


def test_public_http_does_not_receive_insecure_persistent_cookie(account_api):
    services, client = account_api
    create_user(services.auth)
    response = client.post("http://122.51.168.205/api/v1/auth/login", json={"email": "customer@example.test", "password": PASSWORD})
    assert response.status_code == 200
    assert response.json()["persistent_session"] is False
    assert "HTTPS" in response.json()["session_notice"]
    assert "set-cookie" not in response.headers
    assert services.auth.verify_token(response.json()["access_token"])


def test_public_http_logout_revokes_its_bearer_without_revoking_other_devices(account_api):
    services, client = account_api
    create_user(services.auth)
    base = "http://cad.example.test/api/v1"
    first = client.post(f"{base}/auth/login", json={"email": "customer@example.test", "password": PASSWORD})
    second = client.post(f"{base}/auth/login", json={"email": "customer@example.test", "password": PASSWORD})
    assert first.status_code == second.status_code == 200
    assert first.json()["persistent_session"] is second.json()["persistent_session"] is False
    assert COOKIE_NAME not in client.cookies
    assert client.get(f"{base}/auth/me", headers=bearer(first)).status_code == 200
    headers = {"X-JoyNiu-CSRF": "1", **bearer(first)}
    assert client.post(f"{base}/auth/logout", json={}, headers=headers).json() == {"ok": True}
    assert client.get(f"{base}/auth/me", headers=bearer(first)).status_code == 401
    assert client.get(f"{base}/auth/me", headers=bearer(second)).status_code == 200
    assert client.post(f"{base}/auth/logout", json={}, headers=headers).status_code == 200


def test_login_and_registration_are_rate_limited(account_api, monkeypatch):
    services, client = account_api
    create_user(services.auth)
    for _ in range(10):
        assert login(client, password="bad-password").status_code == 401
    limited = login(client)
    assert limited.status_code == 429 and int(limited.headers["retry-after"]) > 0
    monkeypatch.setenv("JOYNIU_PUBLIC_REGISTRATION", "true")
    for _ in range(5):
        assert client.post("/api/v1/auth/register", json={"roles": ["admin"]}, headers=SECURE).status_code == 422
    assert client.post("/api/v1/auth/register", json={}, headers=SECURE).status_code == 429


def test_sessions_and_hashes_survive_restart_and_absolute_expiry(tmp_path):
    database = tmp_path / "accounts.sqlite"
    now = [1000]
    first = AuthService(database, token_secret=SECRET, clock=lambda: now[0])
    user = create_user(first)
    token, raw, expiry = first.create_session(user, ttl_seconds=600)
    assert expiry == token.expires_at == 1600
    original_hash = first._connection.execute("SELECT password_hash FROM users WHERE id=?", (user.id,)).fetchone()[0]
    first.close()
    second = AuthService(database, token_secret=SECRET, clock=lambda: now[0])
    assert second._connection.execute("SELECT password_hash FROM users WHERE id=?", (user.id,)).fetchone()[0] == original_hash
    assert second.verify_token(token.token).id == user.id
    now[0] = 1500
    fresh, next_raw, next_expiry = second.refresh_session(raw)
    assert next_expiry == 1600
    assert fresh.expires_at == 1600
    database_text = "\n".join(second._connection.iterdump())
    assert raw not in database_text and next_raw not in database_text
    assert hashlib.sha256(raw.encode()).hexdigest() in database_text
    now[0] = 1600
    with pytest.raises(AuthenticationError):
        second.refresh_session(next_raw)
    with pytest.raises(AuthenticationError):
        second.verify_token(fresh.token)
    second.close()


def test_legacy_database_migration_preserves_users_passwords_roles_and_tokens(tmp_path):
    database = tmp_path / "legacy.sqlite"
    first = AuthService(database, token_secret=SECRET)
    user = create_user(first, roles=("admin", "designer"))
    token = first.authenticate(user.email, PASSWORD)
    before = first._connection.execute("SELECT * FROM users WHERE id=?", (user.id,)).fetchone()
    first.close()
    with sqlite3.connect(database) as connection:
        for table in ["auth_refresh_tokens", "auth_sessions", "auth_user_security", "auth_rate_limits"]:
            connection.execute(f"DROP TABLE {table}")
    migrated = AuthService(database, token_secret=SECRET)
    assert tuple(migrated._connection.execute("SELECT * FROM users WHERE id=?", (user.id,)).fetchone()) == tuple(before)
    assert migrated.get_user(user.id).roles == user.roles
    assert migrated.verify_token(token.token).id == user.id
    assert migrated.authenticate(user.email, PASSWORD).user.id == user.id
    migrated.close()


def test_deactivation_revokes_sessions_permanently_and_reset_requires_active_admin():
    auth = AuthService(token_secret=SECRET)
    user = create_user(auth)
    admin = create_user(auth, "admin@example.test", ("admin",))
    token, raw, _ = auth.create_session(user)
    auth.set_active(user.id, False, actor_id=admin.id)
    auth.set_active(user.id, True, actor_id=admin.id)
    with pytest.raises(AuthenticationError):
        auth.verify_token(token.token)
    with pytest.raises(AuthenticationError):
        auth.refresh_session(raw)
    with pytest.raises(AuthorizationError):
        auth.reset_password(admin.id, NEXT_PASSWORD, actor_id=user.id)
    create_user(auth, "retained-admin@example.test", ("admin",))
    auth.set_active(admin.id, False, actor_id=admin.id)
    with pytest.raises(AuthorizationError):
        auth.reset_password(user.id, NEXT_PASSWORD, actor_id=admin.id)
    auth.close()


def test_old_credentials_cannot_create_a_new_session_after_password_reset():
    auth = AuthService(token_secret=SECRET)
    user = create_user(auth)
    authenticated = auth.authenticate(user.email, PASSWORD)
    auth.change_password(user.id, PASSWORD, NEXT_PASSWORD)
    with pytest.raises(AuthenticationError):
        auth.create_session(user, credential_token=authenticated.token)
    auth.close()


def test_limits_persist_between_workers_and_expire(tmp_path):
    now = [1000]
    first = AuthService(tmp_path / "limits.sqlite", token_secret=SECRET, clock=lambda: now[0])
    second = AuthService(tmp_path / "limits.sqlite", token_secret=SECRET, clock=lambda: now[0])
    first.consume_auth_limits([("login", "customer", 2, 60)])
    second.consume_auth_limits([("login", "customer", 2, 60)])
    with pytest.raises(RateLimitError) as raised:
        first.consume_auth_limits([("login", "customer", 2, 60)])
    assert raised.value.retry_after == 60
    now[0] = 1060
    second.consume_auth_limits([("login", "customer", 2, 60)])
    first.close()
    second.close()


def test_concurrent_refresh_is_atomic_across_workers(tmp_path):
    first = AuthService(tmp_path / "sessions.sqlite", token_secret=SECRET)
    second = AuthService(tmp_path / "sessions.sqlite", token_secret=SECRET)
    user = create_user(first)
    _, raw, _ = first.create_session(user)
    def refresh(auth):
        try:
            return auth.refresh_session(raw)
        except AuthenticationError:
            return None
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(refresh, [first, second]))
    assert len([result for result in results if result]) == 1
    winner = next(result for result in results if result)
    with pytest.raises(AuthenticationError):
        first.verify_token(winner[0].token)
    first.close()
    second.close()
