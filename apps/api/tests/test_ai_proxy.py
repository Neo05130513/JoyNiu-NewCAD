from __future__ import annotations

import json

import pytest

fastapi = pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.ai_proxy import AIConversationResult, AIProxy, AIProxyError  # noqa: E402
from app.platform_api import build_platform_services, create_platform_router  # noqa: E402


class _Response:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit=-1):
        return self.payload


def test_proxy_sends_responses_schema_reasoning_and_previous_id(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.data.decode())
        return _Response(
            {
                "id": "resp_test_123",
                "output_text": json.dumps(
                    {
                        "message": "已将长度改为 80 mm",
                        "parameter_patch": {"length": 80},
                        "needs_review": False,
                        "questions": [],
                    }
                ),
            }
        )

    monkeypatch.setenv("JOYNIU_AI_API_KEY", "test-provider-key")
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    result = AIProxy(timeout_seconds=7).converse(
        "把轴加长到 80",
        previous_response_id="resp_previous_1",
        model_state={"kind": "shaft", "length": 70},
    )

    assert result.response_id == "resp_test_123"
    assert result.parameter_patch == {"length": 80}
    assert captured["url"] == "https://gptx.shop/v1/responses"
    assert captured["timeout"] == 7
    assert captured["body"]["model"] == "gpt-5.6-sol"
    assert captured["body"]["reasoning"] == {"effort": "high"}
    assert captured["body"]["previous_response_id"] == "resp_previous_1"
    assert captured["body"]["text"]["format"]["strict"] is True
    assert captured["body"]["text"]["format"]["name"] == "joyniu_cad_parameter_patch"
    # The credential is only an Authorization header and is not put in the
    # model input, structured result, or any client-facing field.
    assert "test-provider-key" not in json.dumps(captured["body"])


def test_proxy_reads_key_file_without_requiring_environment(monkeypatch, tmp_path):
    key_file = tmp_path / "provider.key"
    key_file.write_text("file-provider-key\n", encoding="utf-8")
    monkeypatch.delenv("JOYNIU_AI_API_KEY", raising=False)
    monkeypatch.setenv("JOYNIU_AI_API_KEY_FILE", str(key_file))
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda request, timeout: _Response(
            {
                "id": "resp_file_key",
                "output_text": '{"message":"ok","parameter_patch":{},"needs_review":false,"questions":[]}',
            }
        ),
    )
    assert AIProxy().converse("继续").response_id == "resp_file_key"


def test_proxy_rejects_unsupported_provider_patch(monkeypatch):
    monkeypatch.setenv("JOYNIU_AI_API_KEY", "test-provider-key")
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda request, timeout: _Response(
            {
                "id": "resp_bad_patch",
                "output_text": '{"message":"bad","parameter_patch":{"shellCommand":"rm"},"needs_review":true,"questions":[]}',
            }
        ),
    )
    with pytest.raises(AIProxyError, match="unsupported parameter"):
        AIProxy().converse("修改模型")


def _client_for(services):
    app = FastAPI()
    app.include_router(create_platform_router(services), prefix="/api/v1")
    return TestClient(app)


def _auth_for(services, user):
    return {"Authorization": f"Bearer {services.auth.issue_token(user).token}"}


def test_ai_route_requires_auth_and_restricts_viewer():
    services = build_platform_services(":memory:", auth_secret="a" * 32)
    viewer = services.auth.create_user("viewer@example.com", "long-password", "Viewer", roles=["viewer"])
    client = _client_for(services)
    assert "/api/v1/ai/conversation" in client.app.openapi()["paths"]
    assert client.post("/api/v1/ai/conversation", data={"message": "修改长度"}).status_code == 401
    assert client.post(
        "/api/v1/ai/conversation",
        headers=_auth_for(services, viewer),
        data={"message": "修改长度"},
    ).status_code == 403


def test_ai_route_passes_file_and_turn_state_to_injected_proxy():
    services = build_platform_services(":memory:", auth_secret="b" * 32)
    designer = services.auth.create_user("designer@example.com", "long-password", "Designer", roles=["designer"])

    class FakeAI:
        def __init__(self):
            self.calls = []

        def converse(self, message, *, previous_response_id, model_state, files):
            self.calls.append((message, previous_response_id, model_state, files))
            return AIConversationResult("resp_fake_next", "已根据图纸更新", {"baseLength": 110}, True, ("请确认单位",))

    fake = FakeAI()
    services.ai = fake
    client = _client_for(services)
    response = client.post(
        "/api/v1/ai/conversation",
        headers=_auth_for(services, designer),
        data={
            "message": "把底板加长",
            "previous_response_id": "resp_previous",
            "model_state_json": json.dumps({"kind": "bracket", "baseLength": 100}),
        },
        files={"file": ("drawing.pdf", b"%PDF-test", "application/pdf")},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "responseId": "resp_fake_next",
        "message": "已根据图纸更新",
        "parameterPatch": {"baseLength": 110},
        "needsReview": True,
        "questions": ["请确认单位"],
    }
    assert fake.calls[0][0] == "把底板加长"
    assert fake.calls[0][1] == "resp_previous"
    assert fake.calls[0][2]["baseLength"] == 100
    assert fake.calls[0][3][0].filename == "drawing.pdf"
    assert fake.calls[0][3][0].data == b"%PDF-test"
