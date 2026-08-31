from __future__ import annotations

import json
import base64
import hashlib

import pytest

fastapi = pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.ai_proxy import AIConversationResult, AIFile, AIProviderNotConfigured, AIProxy, AIProxyError  # noqa: E402
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


def test_provider_image_normalization_keeps_original_audit_metadata():
    """Vision relays receive JPEG while the uploaded PNG identity is unchanged."""
    from app.ai_proxy import _attachment_content

    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
        "+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    metadata, attachment = _attachment_content(AIFile("drawing.png", "image/png", png))
    assert metadata["contentType"] == "image/png"
    assert metadata["sizeBytes"] == len(png)
    assert metadata["sha256"] == hashlib.sha256(png).hexdigest()
    assert attachment["type"] == "input_image"
    assert attachment["image_url"].startswith("data:image/jpeg;base64,")


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


def test_proxy_parses_markdown_key_file(monkeypatch, tmp_path):
    """Deployment notes may be Markdown; send only the code-block token."""
    key_file = tmp_path / "provider.md"
    key_file.write_text(
        "# Provider credential\n\n```text\nsk-" + "A" * 24 + "\n```\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("JOYNIU_AI_API_KEY", raising=False)
    monkeypatch.setenv("JOYNIU_AI_API_KEY_FILE", str(key_file))
    captured = {}

    def fake_urlopen(request, timeout):
        captured["authorization"] = request.headers["Authorization"]
        return _Response(
            {
                "id": "resp_markdown_key",
                "output_text": '{"message":"ok","parameter_patch":{},"needs_review":false,"questions":[]}',
            }
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    assert AIProxy().converse("继续").response_id == "resp_markdown_key"
    assert captured["authorization"] == "Bearer sk-" + "A" * 24


def test_proxy_parses_non_openai_markdown_token_and_respects_config_priority(monkeypatch, tmp_path):
    """GPTX deployments may use a non-sk token in a fenced key note."""
    from app.ai_proxy import _provider_key

    key_file = tmp_path / "provider.md"
    key_file.write_text("# relay\n\n```text\nrelay-token-" + "B" * 20 + "\n```\n", encoding="utf-8")
    monkeypatch.delenv("JOYNIU_LLM_API_KEY", raising=False)
    monkeypatch.delenv("JOYNIU_AI_API_KEY", raising=False)
    monkeypatch.setenv("JOYNIU_AI_API_KEY_FILE", str(key_file))
    assert _provider_key() == "relay-token-" + "B" * 20

    # The explicit LLM compatibility name is canonical when both are set.
    monkeypatch.setenv("JOYNIU_LLM_API_KEY", "llm-priority-token")
    monkeypatch.setenv("JOYNIU_AI_API_KEY", "ai-priority-token")
    assert _provider_key() == "llm-priority-token"


def test_proxy_does_not_consume_generic_openai_environment_key(monkeypatch):
    """A generic key must not be forwarded to the GPTX relay by accident."""
    from app.ai_proxy import _provider_key

    monkeypatch.delenv("JOYNIU_LLM_API_KEY", raising=False)
    monkeypatch.delenv("JOYNIU_AI_API_KEY", raising=False)
    monkeypatch.delenv("JOYNIU_LLM_API_KEY_FILE", raising=False)
    monkeypatch.delenv("JOYNIU_AI_API_KEY_FILE", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "generic-key-must-not-forward")
    with pytest.raises(AIProviderNotConfigured) as error:
        _provider_key()
    assert "generic-key-must-not-forward" not in str(error.value)


def test_unknown_drawing_returns_review_envelope_without_provider(monkeypatch):
    """Missing relay configuration must not turn unknown evidence into 503."""
    from app import ai_proxy

    for name in (
        "JOYNIU_LLM_API_KEY", "JOYNIU_AI_API_KEY", "JOYNIU_LLM_API_KEY_FILE",
        "JOYNIU_AI_API_KEY_FILE", "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        ai_proxy,
        "_recognize_attachment",
        lambda _item: {"id": "compat_unknown", "status": "needs_review", "parameters": {}},
    )
    result = AIProxy().converse(
        "解析这份图纸",
        files=(AIFile("unknown.dwg", "application/octet-stream", b"AC10"),),
    )
    assert result.needs_review is True
    assert result.parameter_patch == {}
    assert result.drawing["id"] == "compat_unknown"


def test_platform_conversation_does_not_promote_compatibility_scaffold(monkeypatch):
    """Legacy canonical preview values stay out of the AI candidate envelope."""
    from app.recognition import canonical_bracket_parameters

    services = build_platform_services(":memory:", auth_secret="c" * 32)
    canonical = canonical_bracket_parameters().model_dump(by_alias=True)
    services.ai.converse = lambda *args, **kwargs: AIConversationResult(
        response_id="local_compat",
        message="候选待确认",
        parameter_patch={},
        needs_review=True,
        questions=("请确认视图与尺寸",),
        drawing={
            "id": "legacy-compat",
            "status": "needs_review",
            "partType": "bracket",
            "engine": "tesseract-compatible",
            "parameters": canonical,
            "modelRecipe": {"parameters": canonical, "source": "compatibility-recognizer"},
        },
        provider={"mode": "local-fallback"},
    )
    monkeypatch.setenv("JOYNIU_AI_ALLOW_ANONYMOUS", "1")
    monkeypatch.setenv("JOYNIU_ENV", "development")
    app = FastAPI()
    app.include_router(create_platform_router(services), prefix="/api/v1")
    client = TestClient(app)
    response = client.post(
        "/api/v1/ai/conversation",
        data={"message": "解析图纸"},
        files={"file": ("unknown.png", b"not-a-real-image", "image/png")},
    )
    assert response.status_code == 200, response.text
    drawing = response.json()["drawingRecognition"]
    assert drawing["status"] == "needs_review"
    assert drawing["partType"] == "unknown"
    assert drawing["parameters"] == {}
    assert drawing["candidateParameters"] == {}


def test_anonymous_ai_requires_loopback_or_development_environment(monkeypatch):
    from types import SimpleNamespace

    from app.platform_api import _anonymous_ai_request_allowed

    remote_request = SimpleNamespace(client=SimpleNamespace(host="10.20.30.40"))
    monkeypatch.delenv("JOYNIU_ENV", raising=False)
    assert _anonymous_ai_request_allowed(remote_request) is False
    monkeypatch.setenv("JOYNIU_ENV", "development")
    assert _anonymous_ai_request_allowed(remote_request) is True



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


def test_verified_drawing_uses_deterministic_patch_without_remote_call(monkeypatch):
    """A reviewed drawing must not be reinterpreted by a flaky relay."""
    from app import ai_proxy
    from app.recognition import canonical_bracket_parameters

    drawing = {
        "status": "confirmed",
        "parameters": canonical_bracket_parameters().model_dump(by_alias=True),
        "id": "drw_verified",
    }
    monkeypatch.setattr(ai_proxy, "_recognize_attachment", lambda _item: drawing)
    monkeypatch.delenv("JOYNIU_AI_API_KEY", raising=False)
    monkeypatch.delenv("JOYNIU_AI_API_KEY_FILE", raising=False)

    def fail_remote(*_args, **_kwargs):
        raise AssertionError("verified fixture should not call the relay")

    monkeypatch.setattr("urllib.request.urlopen", fail_remote)
    result = AIProxy().converse(
        "请生成三维模型",
        files=(AIFile("drawing.jpg", "image/jpeg", b"fixture"),),
    )
    assert result.provider["mode"] == "verified-local"
    assert result.needs_review is False
    assert result.parameter_patch["upperWidth"] == 50
    assert result.parameter_patch["slotLength"] == 30


def test_unreviewed_attachment_keeps_review_gate_for_local_edit(monkeypatch):
    """An explicit local edit must not silently confirm an unknown drawing."""
    from app import ai_proxy

    monkeypatch.setattr(
        ai_proxy,
        "_recognize_attachment",
        lambda _item: {"status": "needs_review", "parameters": {}},
    )
    monkeypatch.delenv("JOYNIU_AI_API_KEY", raising=False)
    monkeypatch.delenv("JOYNIU_AI_API_KEY_FILE", raising=False)
    result = AIProxy().converse(
        "把底板长度改为 110 mm",
        model_state={"kind": "bracket"},
        files=(AIFile("drawing.pdf", "application/pdf", b"pdf"),),
    )
    assert result.parameter_patch == {"baseLength": 110}
    assert result.needs_review is True


def test_local_dimension_grammar_prefers_target_value_and_separates_keyway_length(monkeypatch):
    """Offline edits should use the new value, not an old dimension token."""
    from app.ai_proxy import _text_parameter_patch

    assert _text_parameter_patch("把底板长度从100改为90", {"kind": "bracket"}) == {"baseLength": 90}
    assert _text_parameter_patch("修改 R15 为 R12", {"kind": "bracket"}) == {"notchRadius": 12}
    assert _text_parameter_patch("把键槽长度改为45", {"kind": "shaft"}) == {"keywayLength": 45}
    assert _text_parameter_patch(
        "把轴长度改为80，同时把键槽长度改为45", {"kind": "shaft"}
    ) == {"length": 80, "keywayLength": 45}


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
    payload = response.json()
    assert payload["responseId"] == "resp_fake_next"
    assert payload["message"] == "已根据图纸更新"
    assert payload["parameterPatch"] == {"baseLength": 110}
    assert payload["needsReview"] is True
    assert payload["questions"] == ["请确认单位"]
    # The id is from the platform OCR service, not the compatibility AI
    # recognizer, so it can be used by /brackets/generate and reviewer routes.
    drawing = payload["drawingRecognition"]
    assert drawing["status"] == "needs_review"
    assert drawing["id"] in services.recognitions
    # This synthetic PDF has no deterministic recipe; the compatibility alias
    # is still present and intentionally empty until reviewer confirmation.
    assert drawing["parameters"] == {}
    assert services.recognitions[drawing["id"]].source_filename == "drawing.pdf"
    assert fake.calls[0][0] == "把底板加长"
    assert fake.calls[0][1] == "resp_previous"
    assert fake.calls[0][2]["baseLength"] == 100
    assert fake.calls[0][3][0].filename == "drawing.pdf"
    assert fake.calls[0][3][0].data == b"%PDF-test"


def test_ai_route_drops_unregistered_compatibility_recognition(monkeypatch):
    services = build_platform_services(":memory:", auth_secret="e" * 32)
    designer = services.auth.create_user("compat@example.com", "long-password", "Designer", roles=["designer"])

    class FakeAI:
        def converse(self, message, *, previous_response_id, model_state, files):
            return AIConversationResult(
                "resp_compat", "收到", {}, True, (),
                drawing={"id": "compat_not_registered", "status": "confirmed"},
            )

    class BrokenOCR:
        def analyze(self, *_args, **_kwargs):
            raise RuntimeError("parser unavailable")

    services.ai = FakeAI()
    services.ocr = BrokenOCR()
    client = _client_for(services)
    response = client.post(
        "/api/v1/ai/conversation",
        headers=_auth_for(services, designer),
        data={"message": "解析"},
        files={"file": ("drawing.dxf", b"0\nSECTION", "application/dxf")},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert "drawingRecognition" not in payload
    assert payload["needsReview"] is True


def test_ai_route_explicit_anonymous_demo_accepts_file_alias(monkeypatch):
    """Guest mode is opt-in and accepts the common singular ``file`` field."""
    services = build_platform_services(":memory:", auth_secret="d" * 32)
    monkeypatch.setenv("JOYNIU_AI_ALLOW_ANONYMOUS", "1")

    class FakeAI:
        allow_anonymous = True

        def converse(self, message, *, previous_response_id, model_state, files):
            assert message == "解析图纸"
            assert len(files) == 1 and files[0].filename == "drawing.dwg"
            return AIConversationResult("local_guest", "已收到", {}, True, ("请确认",))

    services.ai = FakeAI()
    client = _client_for(services)
    response = client.post(
        "/api/v1/ai/chat",
        data={"message": "解析图纸"},
        files={"file": ("drawing.dwg", b"AC10", "application/octet-stream")},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["responseId"] == "local_guest"
    assert payload["attachments"][0]["filename"] == "drawing.dwg"
    assert payload["drawingRecognition"]["status"] == "needs_review"


def test_ai_chat_alias_preserves_camel_case_turn_state():
    services = build_platform_services(":memory:", auth_secret="f" * 32)
    designer = services.auth.create_user("camel@example.com", "long-password", "Designer", roles=["designer"])

    class FakeAI:
        allow_anonymous = False

        def __init__(self):
            self.call = None

        def converse(self, message, *, previous_response_id, model_state, files):
            self.call = (message, previous_response_id, model_state)
            return AIConversationResult("resp_camel", "ok", {}, False, ())

    fake = FakeAI()
    services.ai = fake
    client = _client_for(services)
    response = client.post(
        "/api/v1/ai/chat",
        headers=_auth_for(services, designer),
        data={
            "message": "继续",
            "previousResponseId": "resp_previous",
            "modelState": json.dumps({"kind": "bracket", "baseLength": 100}),
        },
    )
    assert response.status_code == 200, response.text
    assert fake.call == ("继续", "resp_previous", {"kind": "bracket", "baseLength": 100})


def test_ai_route_rejects_malformed_model_state_as_validation_error():
    services = build_platform_services(":memory:", auth_secret="g" * 32)
    designer = services.auth.create_user("bad-state@example.com", "long-password", "Designer", roles=["designer"])

    class FakeAI:
        allow_anonymous = False

        def converse(self, **_kwargs):  # pragma: no cover - must not be called
            raise AssertionError("malformed state should be rejected before AI invocation")

    services.ai = FakeAI()
    client = _client_for(services)
    response = client.post(
        "/api/v1/ai/conversation",
        headers=_auth_for(services, designer),
        data={"message": "继续", "model_state_json": "{not-json"},
    )
    assert response.status_code == 422, response.text


def test_platform_recognition_id_is_accepted_by_geometry_generate(monkeypatch):
    """AI-upload ids must cross the platform/legacy geometry boundary."""
    from types import SimpleNamespace

    from app import main as geometry_main

    services = build_platform_services(":memory:", auth_secret="c" * 32)
    drawing_id = "drw_platform_confirmed"
    services.recognitions[drawing_id] = SimpleNamespace(
        id=drawing_id,
        status="confirmed",
        source_sha256="unavailable-for-this-regression",
    )
    monkeypatch.setattr(geometry_main, "platform_services", services)
    client = TestClient(geometry_main.app)
    response = client.post(
        "/api/brackets/generate",
        json={
            "sourceDrawingId": drawing_id,
            "formats": ["glb"],
            "parameters": {
                "baseLength": 100,
                "baseWidth": 50,
                "baseThickness": 10,
                "upperLength": 70,
                "upperWidth": 50,
                "upperHeight": 30,
                "totalHeight": 40,
                "notchOpening": 40,
                "notchRadius": 15,
                "slotLength": 30,
                "slotWidth": 10,
                "pocketDepth": 10,
                "saddleDepth": 50,
                "holeDepth": 40,
                "holeThrough": True,
                "bossDiameter": 20,
                "bossCenterDistance": 70,
            },
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["sourceDrawingId"] == drawing_id
