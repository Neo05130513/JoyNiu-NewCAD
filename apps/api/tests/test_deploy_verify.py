"""Deployment smoke checks use status fixtures only; no servers or AI calls."""
import copy
import importlib.util
import json
from pathlib import Path

import pytest


VERIFY_PATH = Path(__file__).resolve().parents[3] / "deploy" / "verify.py"
spec = importlib.util.spec_from_file_location("joyniu_deployment_verify", VERIFY_PATH)
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)


RELAY = {"mode": "remote", "model": "gpt-5.6-sol", "configured": True, "anonymousAllowed": False}
CODEX = {"mode": "codex", "model": "gpt-6-astra", "configured": True,
         "authentication": "cli-managed"}


def fake_http(monkeypatch, ai_status):
    calls = []
    def homepage(self):
        self.result("homepage", True, 200)
    def fetch(self, name, path, *, expected=200, payload=None, token=None):
        calls.append((path, payload, token))
        if expected == 401:
            return 401, "application/json", b'{}'
        data = {
            "api/v1/health": {"status": "ok", **{k: {"available": True} for k in ("geometry", "dwg", "pdf")}},
            "api/v1/ai/status": ai_status,
            "api/v1/cad-agent/capabilities": {"version": "cad-agent-v1", "formats": ["step", "glb"]},
            "api/v1/auth/status": {"initialized": True, "bootstrapAllowed": False},
        }[path]
        return 200, "application/json", json.dumps(data).encode()
    monkeypatch.setattr(verify.Verifier, "homepage", homepage)
    monkeypatch.setattr(verify.Verifier, "fetch", fetch)
    return calls


def checks(monkeypatch, status, *, mode=None, model=None):
    calls = fake_http(monkeypatch, status)
    verifier = verify.Verifier("http://test.invalid", 20, expected_cad_provider=mode, expected_cad_model=model)
    verifier.run()
    assert all(payload is None and token is None for _, payload, token in calls)
    assert [path for path, _, _ in calls].count("api/v1/ai/status") == 1
    return {item["name"]: item for item in verifier.checks}


def test_unset_expectations_preserve_previous_relay_checks(monkeypatch):
    status = {**RELAY, "cadProvider": {"mode": "codex", "configured": False}}
    result = checks(monkeypatch, status)
    assert all(item["ok"] for item in result.values())
    assert "cad_provider_selection" not in result


def test_expected_codex_uses_selected_model_and_does_not_require_unused_relay_key(monkeypatch):
    status = {**RELAY, "mode": "local-fallback", "configured": False, "cadProvider": CODEX}
    result = checks(monkeypatch, status, mode="codex", model="gpt-6-astra")
    assert all(item["ok"] for item in result.values())
    assert result["cad_binary"]["ok"] and "cad_authentication" not in result


def test_configured_relay_cannot_mask_wrong_cad_engine(monkeypatch):
    result = checks(monkeypatch, RELAY, mode="codex", model="gpt-6-astra")
    assert result["ai_configuration"]["ok"]
    assert result["cad_provider_selection"]["error"] == "cad_provider_mismatch"
    assert result["cad_provider_model"]["error"] == "cad_model_mismatch"


def test_expected_relay_cannot_read_top_level_model_when_codex_is_selected(monkeypatch):
    result = checks(monkeypatch, {**RELAY, "cadProvider": CODEX}, mode="relay", model="gpt-5.6-sol")
    assert result["cad_provider_selection"]["error"] == "cad_provider_mismatch"
    assert result["cad_provider_model"]["error"] == "cad_model_mismatch"


def test_expected_relay_accepts_existing_status_without_nested_provider(monkeypatch):
    result = checks(monkeypatch, RELAY, mode="relay", model="gpt-5.6-sol")
    assert all(item["ok"] for item in result.values())
    assert "cad_binary" not in result and "cad_authentication" not in result


@pytest.mark.parametrize("field,value,check,error", [
    ("binaryAvailable", False, "cad_binary", "cad_binary_unavailable"),
    ("binaryAvailable", "true", "cad_binary", "cad_binary_status_unavailable"),
])
def test_codex_honors_explicit_binary_availability_when_reported(monkeypatch, field, value, check, error):
    selected = {**CODEX, field: value}
    if value is None:
        selected.pop(field)
    result = checks(monkeypatch, {**RELAY, "cadProvider": selected}, mode="codex")
    assert result[check]["error"] == error


@pytest.mark.parametrize("selected", [None, [], {}, {"mode": {}}, {"mode": "mystery"}])
def test_malformed_selected_provider_never_falls_back_to_general_relay(monkeypatch, selected):
    result = checks(monkeypatch, {**RELAY, "cadProvider": selected}, mode="relay")
    assert result["cad_provider_selection"]["error"] == "cad_provider_status_unavailable"


def test_model_only_expectation_still_checks_codex_readiness(monkeypatch):
    result = checks(monkeypatch, {**RELAY, "cadProvider": {**CODEX, "configured": False}}, model="gpt-6-astra")
    assert result["cad_provider_model"]["ok"]
    assert result["cad_provider_configuration"]["error"] == "cad_provider_not_configured"
    assert result["cad_binary"]["error"] == "cad_binary_unavailable"


def test_relay_configuration_and_anonymous_protection_remain_required(monkeypatch):
    result = checks(monkeypatch, {**RELAY, "configured": False}, mode="relay")
    assert result["cad_provider_configuration"]["error"] == "cad_provider_not_configured"
    result = checks(monkeypatch, {**RELAY, "anonymousAllowed": True, "cadProvider": CODEX}, mode="codex")
    assert result["ai_configuration"]["ok"] is False


def test_environment_expectations_and_cli_override(monkeypatch, capsys):
    monkeypatch.setenv("JOYNIU_EXPECTED_CAD_PROVIDER", "relay")
    monkeypatch.setenv("JOYNIU_EXPECTED_CAD_MODEL", "gpt-5.6-sol")
    fake_http(monkeypatch, {**RELAY, "cadProvider": CODEX})
    assert verify.main(["--base-url", "http://test.invalid"]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
    assert verify.main(["--base-url", "http://test.invalid", "--expected-cad-provider", "codex",
                        "--expected-cad-model", "gpt-6-astra"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is True
    assert report["unverifiedChecks"] == [{"name": "cad_cli_authentication", "status": "unknown", "reason": "cli_login_precheck_required"}]
    assert not any(item["name"] == "cad_authentication" for item in report["checks"])


def test_empty_environment_expectations_keep_legacy_output(monkeypatch, capsys):
    monkeypatch.setenv("JOYNIU_EXPECTED_CAD_PROVIDER", " ")
    monkeypatch.setenv("JOYNIU_EXPECTED_CAD_MODEL", "")
    fake_http(monkeypatch, RELAY)
    assert verify.main([]) == 0
    report = json.loads(capsys.readouterr().out)
    assert set(report) == {"ok", "authenticatedChecksRequested", "checks"}
    assert not any(item["name"] == "cad_provider_selection" for item in report["checks"])


@pytest.mark.parametrize("option,bad", [("--expected-cad-provider", "SECRET invalid provider"),
                                       ("--expected-cad-model", "SECRET invalid model")])
def test_bad_expectations_fail_before_requests_without_echoing_input(monkeypatch, capsys, option, bad):
    calls = fake_http(monkeypatch, RELAY)
    assert verify.main([option, bad]) == 1
    output = capsys.readouterr().out
    assert "SECRET" not in output and calls == []
    assert json.loads(output)["checks"][0]["name"] == "configuration"


def test_status_bodies_and_credentials_never_appear_in_output(monkeypatch, capsys):
    selected = copy.deepcopy(CODEX)
    selected.update({"authenticated": False, "token": "SECRET_SERVER_TOKEN", "model": "SECRET_SERVER_MODEL"})
    fake_http(monkeypatch, {**RELAY, "cadProvider": selected, "key": "SECRET_SERVER_KEY"})
    assert verify.main(["--expected-cad-mode", "codex", "--expected-cad-model", "gpt-6-astra"]) == 1
    output = capsys.readouterr().out
    assert "SECRET" not in output
    assert all(set(item) <= {"name", "ok", "httpStatus", "error"} for item in json.loads(output)["checks"])


def test_new_workspaces_are_opt_in_and_four_real_routes_are_checked(monkeypatch, capsys):
    calls = fake_http(monkeypatch, RELAY)
    assert verify.main([]) == 0
    capsys.readouterr()
    assert not any(path.startswith('api/cad/') for path, _, _ in calls)
    calls.clear()
    assert verify.main(['--expected-cad-workspaces']) == 0
    report = json.loads(capsys.readouterr().out)
    assert {path for path, _, _ in calls if path.startswith('api/cad/')} == {
        'api/cad/drawings', 'api/cad/features', 'api/cad/designs', 'api/cad/deliveries'}
    assert all(item['httpStatus'] == 401 for item in report['checks'] if item['name'] in {
        'anonymous_native_drawing', 'anonymous_feature_workspace', 'anonymous_engineering_workspace', 'anonymous_delivery_center'})


@pytest.mark.parametrize('bad_status', [200, 302, 404, 500])
def test_missing_or_unprotected_new_workspace_fails(monkeypatch, bad_status):
    fake_http(monkeypatch, RELAY)
    original = verify.Verifier.fetch
    def fetch(self, name, path, **kwargs):
        if path == 'api/cad/deliveries':
            return bad_status, 'application/json', b'{}'
        return original(self, name, path, **kwargs)
    monkeypatch.setattr(verify.Verifier, 'fetch', fetch)
    verifier = verify.Verifier('http://test.invalid', 20, expected_cad_workspaces=True)
    verifier.run()
    failure = next(item for item in verifier.checks if item['name'] == 'anonymous_delivery_center')
    assert not failure['ok'] and failure['httpStatus'] == bad_status
