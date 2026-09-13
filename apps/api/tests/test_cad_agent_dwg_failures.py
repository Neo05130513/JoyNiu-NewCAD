"""DWG conversion failures stay distinct from AI failures at the run boundary."""
from __future__ import annotations

import hashlib
import json

import pytest

from app.ai_proxy import AIFile
from app.cad_agent import CadAgentService
from app.dwg_preprocessor import DWGConverterUnavailableError, DWGParseError


@pytest.mark.parametrize("error,code,reason", [
    (DWGParseError, "dxf_parse_failed", "DWG 转换后的图纸数据无法读取"),
    (DWGConverterUnavailableError, "dwg_converter_unavailable", "服务器的 DWG 转换组件暂不可用"),
])
def test_dwg_failure_keeps_source_identity_and_never_calls_model(monkeypatch, tmp_path, error, code, reason):
    source = AIFile("part.dwg", "application/octet-stream", b"AC1032original-source")
    calls = []
    events = []

    def fail_preprocessing(*_args, **_kwargs):
        raise error("unsafe converter stderr at /private/drawing-content.dxf")

    monkeypatch.setattr("app.dwg_preprocessor.preprocess_dwg", fail_preprocessing)
    result = CadAgentService(provider_call=lambda *_: calls.append(True)).run(
        message="依据图纸生成三维模型", files=(source,), output_dir=tmp_path, progress=events.append)

    assert calls == []
    assert result["status"] == "failed"
    assert result["provider"]["lastErrorCode"] == code
    assert result["provider"]["attempts"] == 0
    assert result["plan"] is None
    assert not result["artifacts"]
    assert not result["drawingReview"]["humanConfirmed"]
    assert result["message"].startswith(reason)
    assert "PDF/PNG" in result["message"]
    assert "管理员" in result["message"]
    assert result["state"]["sourceFiles"][0]["sha256"] == hashlib.sha256(source.data).hexdigest()
    assert result["state"]["sourceFiles"][0]["filename"] == "part.dwg"
    persisted = json.loads((tmp_path / "agent-state.json").read_text())
    assert persisted["message"] == result["message"]
    assert events[-1]["message"] == result["message"]
    assert "unsafe" not in json.dumps(result)
    assert "drawing-content" not in json.dumps(result)
    assert "损坏" not in result["message"]
