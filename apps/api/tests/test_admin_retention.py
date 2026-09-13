from concurrent.futures import ThreadPoolExecutor
import pytest
from app.platform import AuthService, ValidationError


@pytest.mark.parametrize("operation", ["deactivate", "demote"])
def test_last_active_admin_is_preserved_across_workers(tmp_path, monkeypatch, operation):
    monkeypatch.setattr(AuthService, "_PBKDF2_ITERATIONS", 1000)
    path = tmp_path / "accounts.sqlite3"
    first = AuthService(path, token_secret="test-admin-guard-secret" * 2)
    second = AuthService(path, token_secret="test-admin-guard-secret" * 2)
    users = [first.create_user(f"a{i}@example.test", "test-password-123", "Admin", roles=["admin"]) for i in range(2)]

    def change(pair):
        service, user = pair
        try:
            if operation == "deactivate":
                service.set_active(user.id, False, actor_id=user.id)
            else:
                service.assign_roles(user.id, ["designer"], actor_id=user.id)
            return True
        except ValidationError:
            return False

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            changed = list(pool.map(change, zip([first, second], users)))
        assert sorted(changed) == [False, True]
        assert len([user for user in first.list_users() if "admin" in user.roles]) == 1
    finally:
        first.close()
        second.close()
