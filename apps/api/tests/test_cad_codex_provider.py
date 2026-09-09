"""Local-only CLI adapter tests: no installed Codex or model invocation."""
from __future__ import annotations

import base64
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from PIL import Image
import pytest

from app import cad_codex_provider as provider
from app.ai_proxy import AIProviderTransportError, _final_output_text


def image_url(color="white", size=(12, 10)):
    stream = io.BytesIO()
    Image.new("RGB", size, color).save(stream, format="PNG")
    return "data:image/png;base64," + base64.b64encode(stream.getvalue()).decode()


def body(*parts):
    return {"instructions": "只看提供的资料。", "reasoning": {"effort": "high"},
            "text": {"format": {"type": "json_object"}},
            "input": [{"role": "user", "content": list(parts) or [{"type": "input_text", "text": "输出JSON"}]}]}


@pytest.fixture
def fake_cli(tmp_path, monkeypatch):
    capture = tmp_path / "capture.json"

    def create(**options):
        path = tmp_path / "fake codex; never shell"
        script = '''import json,os,sys,time,subprocess,hashlib
from pathlib import Path
options = OPTIONS
args = sys.argv[1:]
if options.get('sleep'):
    time.sleep(options['sleep'])
if options.get('child_heartbeat'):
    subprocess.Popen([sys.executable, '-c', "import time; from pathlib import Path; p=Path("+repr(options['child_heartbeat'])+");\\nwhile True: p.write_text(str(time.monotonic())); time.sleep(0.025)"])
    time.sleep(20)
prompt = sys.stdin.read()
directory = Path(args[args.index('-C')+1])
images = [args[i+1] for i,v in enumerate(args) if v == '-i']
record = {'args':args, 'env':dict(os.environ), 'cwd':str(Path.cwd()), 'prompt':prompt,
          'instructions':(directory/'instructions.txt').read_text(),
          'files':sorted(p.name for p in directory.iterdir()),
          'images':[{'name':Path(p).name,'sha256':hashlib.sha256(Path(p).read_bytes()).hexdigest()} for p in images]}
if '--output-schema' in args:
    record['schema'] = json.loads(Path(args[args.index('--output-schema')+1]).read_text())
Path(CAPTURE).write_text(json.dumps(record,ensure_ascii=False))
final = options.get('final', '{"ok":true,"说明":"主视图标注"}')
target = Path(args[args.index('-o')+1])
if options.get('symlink'):
    target.symlink_to(options['symlink'])
elif not options.get('no_file'):
    target.write_text(final,encoding='utf-8')
events = options.get('events', [
    {'type':'thread.started','thread_id':'private-thread-id'},
    {'type':'turn.started'},
    {'type':'item.completed','item':{'type':'reasoning','text':'HIDDEN REASONING SECRET'}},
    {'type':'item.completed','item':{'type':'agent_message','text':options.get('message',final)}},
    {'type':'turn.completed','usage':{'input_tokens':70,'cached_input_tokens':10,'output_tokens':12,'reasoning_output_tokens':8,'secret':'usage-secret'}}])
if options.get('post_events'):
    events += options['post_events']
raw = options.get('raw')
if raw is None:
    raw = '\\n'.join(json.dumps(e,ensure_ascii=False) for e in events)+'\\n'
if options.get('no_newline'):
    raw = raw.rstrip('\\n')
encoded = raw.encode('utf-8')
chunk = options.get('chunk', len(encoded) or 1)
for index in range(0,len(encoded),chunk):
    os.write(1,encoded[index:index+chunk])
    if options.get('delay'):
        time.sleep(options['delay'])
os.write(2, options.get('stderr','PRIVATE STDERR SECRET').encode('utf-8'))
sys.exit(options.get('exit',0))
'''
        script = script.replace("OPTIONS", repr(options)).replace("CAPTURE", repr(str(capture)))
        path.write_text(f"#!{Path(sys.executable).resolve()}\n" + script)
        path.chmod(0o700)
        monkeypatch.setenv("JOYNIU_CAD_CODEX_BINARY", str(path))
        return capture
    return create


def test_ordered_images_labels_unicode_and_safe_environment(fake_cli, monkeypatch):
    capture = fake_cli(chunk=1)
    monkeypatch.setenv("JOYNIU_AI_API_KEY", "sk-never-forward")
    monkeypatch.setenv("OPENAI_API_KEY", "api-never-forward")
    monkeypatch.setenv("CODEX_API_KEY", "codex-never-forward")
    monkeypatch.setenv("SECRET_EXAMPLE", "secret-never-forward")
    monkeypatch.setenv("JOYNIU_CAD_CODEX_MODEL", "gpt-6-astra")
    snapshots = []
    popen = subprocess.Popen
    launches = []

    def recorded(*args, **kwargs):
        launches.append((args, kwargs))
        return popen(*args, **kwargs)
    monkeypatch.setattr(provider.subprocess, "Popen", recorded)
    result = provider.call(body(
        {"type": "input_text", "text": "原图 source-0："}, {"type": "input_image", "image_url": image_url()},
        {"type": "input_text", "text": "局部 detail-0：厚度区域"}, {"type": "input_image", "image_url": image_url("black")},
        {"type": "input_text", "text": "比较以上图片。$(never execute); `nothing`"}), 5, snapshots.append)
    assert json.loads(_final_output_text(result))["说明"] == "主视图标注"
    record = json.loads(capture.read_text())
    prompt = record["prompt"]
    assert prompt.index("source-0") < prompt.index("[Attached image 1:") < prompt.index("detail-0") < prompt.index("[Attached image 2:") < prompt.index("比较以上")
    assert [entry["name"] for entry in record["images"]] == ["image-001.png", "image-002.png"]
    assert record["images"][0]["sha256"] != record["images"][1]["sha256"]
    assert set(launches[0][1]["env"]) <= provider._ENV_KEYS
    assert launches[0][1]["shell"] is False and launches[0][1]["start_new_session"] is True
    assert Path(record["cwd"]).resolve() == Path(record["args"][record["args"].index("-C") + 1]).resolve()
    assert not Path(record["cwd"]).exists()
    assert record["files"] == ["image-001.png", "image-002.png", "instructions.txt"]
    assert "只看提供的资料" in record["instructions"] and "Do not call tools" in record["instructions"]
    for flag in ("--ephemeral", "--ignore-user-config", "--skip-git-repo-check", "--json"):
        assert flag in record["args"]
    for config in ('features.shell_tool=false', 'features.apps=false', 'features.multi_agent=false',
                   'project_doc_max_bytes=0', 'mcp_servers={}', 'skills.include_instructions=false',
                   'features.skip_host_skill_discovery=true', 'web_search="disabled"'):
        assert config in record["args"]
    assert 'model_reasoning_effort="high"' in record["args"]
    assert result["usage"] == {"input_tokens": 70, "cached_tokens": 10, "output_tokens": 12, "reasoning_tokens": 8}
    serialized = json.dumps([snapshots, result])
    for private in ("PRIVATE STDERR", "HIDDEN REASONING", "private-thread-id", "usage-secret", "sk-never-forward"):
        assert private not in serialized
    assert snapshots[-1]["terminalStatus"] == "completed"
    assert snapshots[-1]["firstByteSeconds"] >= 0
    assert snapshots[-1]["firstOutputTextSeconds"] >= 0


def test_schema_is_applied_verbatim_and_system_is_instruction(fake_cli):
    capture = fake_cli(final='{"count":2}')
    request = body()
    request["input"].insert(0, {"role": "system", "content": "精确读取原图"})
    schema = {"type": "object", "properties": {"count": {"type": "integer"}}, "required": ["count"], "additionalProperties": False}
    request["text"] = {"format": {"type": "json_schema", "name": "count", "schema": schema, "strict": True}}
    assert provider.call(request, 5)["status"] == "completed"
    record = json.loads(capture.read_text())
    assert record["schema"] == schema
    assert "SYSTEM INSTRUCTIONS:\n精确读取原图" in record["instructions"]
    assert "精确读取原图" not in record["prompt"]


@pytest.mark.parametrize("options,code", [
    ({"exit": 7}, "process_failed"),
    ({"post_events": [{"type": "turn.failed", "error": {"message": "PRIVATE FAIL REASON"}}]}, "turn_failed"),
    ({"post_events": [{"type": "error", "message": "PRIVATE ERROR"}]}, "turn_failed"),
    ({"events": [{"type": "turn.cancelled"}]}, "turn_failed"),
    ({"events": [{"type": "item.completed", "item": {"type": "error", "message": "PRIVATE STARTUP FAILURE"}}]}, "process_failed"),
    ({"events": [{"type": "item.completed", "item": {"type": "agent_message", "text": "{}"}}]}, "missing_terminal"),
    ({"events": [{"type": "turn.completed"}]}, "missing_final"),
    ({"events": []}, "missing_terminal"),
    ({"raw": "not JSON\n"}, "invalid_stream"),
    ({"raw": '{"type":"turn.completed","type":"turn.started"}\n'}, "invalid_stream"),
    ({"events": [{"type": "future.success", "text": "{}"}]}, "invalid_stream"),
    ({"no_file": True}, "missing_final"),
    ({"message": '{"different":true}'}, "missing_final"),
    ({"final": '{"ok":true}\n{"ok":false}'}, "invalid_json"),
    ({"final": '```json\n{"ok":true}\n```'}, "invalid_json"),
    ({"final": '{"number":NaN}'}, "invalid_json"),
    ({"final": '{"number":1e999}'}, "invalid_json"),
    ({"final": '{"ok":true,"ok":false}'}, "invalid_json"),
    ({"final": '[]'}, "invalid_json"),
])
def test_fail_closed_and_no_private_error_leak(fake_cli, options, code):
    capture = fake_cli(**options)
    snapshots = []
    with pytest.raises(provider.CodexProviderError) as error:
        provider.call(body(), 5, snapshots.append)
    assert error.value.code == code
    assert "PRIVATE" not in str(error.value) + json.dumps(error.value.diagnostics) + json.dumps(snapshots)
    assert error.value.diagnostics["errorCategory"] == code
    assert not Path(json.loads(capture.read_text())["cwd"]).exists()


@pytest.mark.parametrize("kind", ["command_execution", "file_change", "mcp_tool_call", "web_search", "tool_call", "plan_update", "unknown_tool"])
@pytest.mark.parametrize("stage", ["item.started", "item.completed"])
def test_every_tool_item_is_rejected(fake_cli, kind, stage):
    fake_cli(events=[{"type": stage, "item": {"type": kind, "text": "PRIVATE TOOL OUTPUT"}}])
    with pytest.raises(provider.CodexProviderError, match="tool_event"):
        provider.call(body(), 5)


def test_no_newline_last_event_is_supported(fake_cli):
    fake_cli(no_newline=True)
    assert provider.call(body(), 5)["status"] == "completed"


def test_timeout_kills_process_group_and_cleans_temp(fake_cli, tmp_path, monkeypatch):
    heartbeat = tmp_path / "child-heartbeat"
    fake_cli(child_heartbeat=str(heartbeat))
    launches = []
    popen = subprocess.Popen

    def recorded(*args, **kwargs):
        launches.append((args, kwargs))
        return popen(*args, **kwargs)
    monkeypatch.setattr(provider.subprocess, "Popen", recorded)
    started = time.monotonic()
    with pytest.raises(AIProviderTransportError) as error:
        provider.call(body(), 2)
    assert time.monotonic() - started < 4
    assert error.value.reason == "timeout"
    assert error.value.diagnostics["errorCategory"] == "timeout"
    assert heartbeat.exists()
    after = heartbeat.read_text()
    time.sleep(.12)
    assert heartbeat.read_text() == after
    assert not Path(launches[0][1]["cwd"]).exists()


@pytest.mark.parametrize("channel", ["stdout", "stderr", "event", "final"])
def test_output_budgets_are_bounded(fake_cli, monkeypatch, channel):
    options = {}
    if channel in {"stdout", "stderr"}:
        monkeypatch.setattr(provider, "MAX_OUTPUT_BYTES", 1000)
        options["raw" if channel == "stdout" else "stderr"] = "x" * 1500
    elif channel == "event":
        monkeypatch.setattr(provider, "MAX_EVENT_BYTES", 100)
    else:
        monkeypatch.setattr(provider, "MAX_FINAL_BYTES", 8)
    fake_cli(**options)
    with pytest.raises(provider.CodexProviderError, match="output_limit"):
        provider.call(body(), 5)


@pytest.mark.parametrize("input_body", [
    {}, {"input": []}, {"input": "x", "tools": [{"type": "web_search"}]},
    {"input": "x", "previous_response_id": "old"}, {"input": "x", "reasoning": {"effort": "--evil"}},
    {"input": [{"role": "tool", "content": "tool output"}]},
    {"input": [{"role": "user", "content": [{"type": "input_file", "file_id": "x"}]}]},
    {"input": [{"role": "user", "content": [{"type": "input_image", "image_url": "https://example.com/a.png"}]}]},
    {"input": [{"role": "user", "content": [{"type": "input_image", "image_url": "data:image/png;base64,invalid"}]}]},
    {"input": "x", "text": {"format": {"type": "json_schema", "schema": {"$ref": "https://example.com/schema.json"}}}},
])
def test_invalid_input_never_launches(fake_cli, monkeypatch, input_body):
    fake_cli()
    monkeypatch.setattr(provider.subprocess, "Popen", lambda *a, **k: pytest.fail("invalid input launched CLI"))
    with pytest.raises(provider.CodexProviderError):
        provider.call(input_body, 5)


@pytest.mark.parametrize("limit", ["images", "text", "bytes", "pixels", "total_pixels", "total_bytes", "schema"])
def test_input_limits_never_launch(fake_cli, monkeypatch, limit):
    fake_cli()
    request = body({"type": "input_image", "image_url": image_url()})
    if limit == "images":
        request["input"][0]["content"] *= 21
    elif limit == "text":
        monkeypatch.setattr(provider, "MAX_TEXT_BYTES", 3)
    elif limit == "bytes":
        monkeypatch.setattr(provider, "MAX_IMAGE_BYTES", 5)
    elif limit == "pixels":
        monkeypatch.setattr(provider, "MAX_IMAGE_PIXELS", 10)
    elif limit == "total_pixels":
        monkeypatch.setattr(provider, "MAX_TOTAL_IMAGE_PIXELS", 130)
        request["input"][0]["content"] *= 2
    elif limit == "total_bytes":
        monkeypatch.setattr(provider, "MAX_TOTAL_IMAGE_BYTES", 5)
    else:
        monkeypatch.setattr(provider, "MAX_SCHEMA_BYTES", 5)
        request["text"] = {"format": {"type": "json_schema", "schema": {"type": "object"}}}
    monkeypatch.setattr(provider.subprocess, "Popen", lambda *a, **k: pytest.fail("oversized input launched CLI"))
    with pytest.raises(provider.CodexProviderError, match="input_limit"):
        provider.call(request, 5)


def test_model_is_one_argv_argument_and_bad_model_is_not_echoed(fake_cli, monkeypatch):
    capture = fake_cli()
    monkeypatch.setenv("JOYNIU_CAD_CODEX_MODEL", "family/gpt-model-v2")
    provider.call(body(), 5)
    args = json.loads(capture.read_text())["args"]
    assert args[args.index("-m") + 1] == "family/gpt-model-v2"
    monkeypatch.setenv("JOYNIU_CAD_CODEX_MODEL", "bad; PRIVATE MODEL SECRET")
    with pytest.raises(provider.CodexProviderError) as error:
        provider.call(body(), 5)
    assert "PRIVATE" not in str(error.value)


def test_status_does_not_launch_or_claim_authenticated(fake_cli, monkeypatch):
    fake_cli()
    monkeypatch.delenv("JOYNIU_CAD_CODEX_MODEL", raising=False)
    monkeypatch.setattr(provider.subprocess, "Popen", lambda *a, **k: pytest.fail("status launched CLI"))
    result = provider.status()
    assert result["configured"] is True and result["authentication"] == "cli-managed"
    assert result["model"] == "gpt-6-astra" and "authenticated" not in result
    monkeypatch.setenv("JOYNIU_CAD_CODEX_BINARY", "/missing/PRIVATE BINARY")
    assert provider.status()["configured"] is False
    with pytest.raises(provider.CodexProviderError, match="not_configured"):
        provider.call(body(), 5)


def test_stderr_and_oserror_are_sanitized(fake_cli, monkeypatch):
    fake_cli()
    def unavailable(*args, **kwargs):
        raise OSError("PRIVATE AUTH MATERIAL")
    monkeypatch.setattr(provider.subprocess, "Popen", unavailable)
    with pytest.raises(provider.CodexProviderError) as error:
        provider.call(body(), 5)
    assert error.value.code == "launch_failed" and "PRIVATE" not in str(error.value)


def test_final_symlink_is_rejected(fake_cli, tmp_path):
    external = tmp_path / "not-final.json"
    external.write_text('{"secret":"PRIVATE"}')
    fake_cli(symlink=str(external))
    with pytest.raises(provider.CodexProviderError, match="missing_final"):
        provider.call(body(), 5)


def test_callback_cannot_modify_result_and_exceptions_are_isolated(fake_cli):
    fake_cli()
    def observer(value):
        value["usage"] = {"output_tokens": "not-safe"}
        raise RuntimeError("private observer failure")
    assert provider.call(body(), 5, observer)["usage"]["output_tokens"] == 12


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), True, "2"])
def test_invalid_timeout_never_launches(fake_cli, monkeypatch, timeout):
    fake_cli()
    monkeypatch.setattr(provider.subprocess, "Popen", lambda *a, **k: pytest.fail("invalid timeout launched CLI"))
    with pytest.raises(provider.CodexProviderError, match="invalid_input"):
        provider.call(body(), timeout)


def test_one_run_keeps_its_selected_executable_and_model(fake_cli, monkeypatch):
    capture = fake_cli()
    monkeypatch.setenv('JOYNIU_CAD_CODEX_MODEL', 'selected-model')
    selected = provider.CodexCadProvider()
    monkeypatch.setenv('JOYNIU_CAD_CODEX_MODEL', 'different-model')
    monkeypatch.setenv('JOYNIU_CAD_CODEX_BINARY', '/missing/replacement')
    result = selected(body(), 5)
    args = json.loads(capture.read_text())['args']
    assert args[args.index('-m') + 1] == 'selected-model'
    assert result['model'] == selected.provider_info['model'] == 'selected-model'
    info = selected.provider_info
    info['model'] = 'caller-mutation'
    assert selected.provider_info['model'] == 'selected-model'
