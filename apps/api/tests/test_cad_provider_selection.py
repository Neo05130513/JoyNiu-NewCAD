from types import SimpleNamespace
import sys

from app import ai_proxy
from app.cad_agent import CadAgentService
from app.cad_provider import cad_provider_status, configured_cad_provider, same_reading_engine


class LocalProvider:
    provider_info = {'name': 'codex-cli', 'mode': 'codex', 'model': 'gpt-6-astra',
                     'reasoningEffort': 'high', 'configured': True, 'authentication': 'cli-managed'}
    supports_diagnostics = True

    def __call__(self, body, timeout, on_diagnostics=None):
        if on_diagnostics:
            on_diagnostics({'outputChars': 2, 'terminalStatus': 'completed'})
        return {'status': 'completed', 'output_text': '{}'}


def install(monkeypatch):
    monkeypatch.setenv('JOYNIU_CAD_PROVIDER', 'codex')
    monkeypatch.setitem(sys.modules, 'app.cad_codex_provider', SimpleNamespace(CodexCadProvider=LocalProvider))


def test_default_keeps_existing_relay(monkeypatch):
    monkeypatch.delenv('JOYNIU_CAD_PROVIDER', raising=False)
    assert configured_cad_provider() is None
    assert cad_provider_status() is None


def test_codex_is_shared_by_reading_spatial_and_planning(monkeypatch):
    install(monkeypatch)
    service = CadAgentService()
    assert isinstance(service.provider_call, LocalProvider)
    assert service.source_reader.provider_call is service.provider_call
    assert service.spatial_interpreter.provider_call is service.provider_call
    diagnostics = []
    assert service._call({}, 2, on_diagnostics=diagnostics.append)['status'] == 'completed'
    assert diagnostics[-1]['terminalStatus'] == 'completed'


def test_explicit_injected_provider_wins(monkeypatch):
    install(monkeypatch)
    custom = lambda body, timeout: {}
    service = CadAgentService(provider_call=custom)
    assert service.provider_call is custom


def test_status_keeps_legacy_chat_and_separate_cad_metadata(monkeypatch):
    install(monkeypatch)
    monkeypatch.setattr(ai_proxy, '_provider_configured', lambda: False)
    monkeypatch.setattr(ai_proxy, '_base_url', lambda: 'https://example.test/v1')
    result = ai_proxy.AIProxy().status()
    assert result['mode'] == 'local-fallback'
    assert result['cadProvider'] == LocalProvider.provider_info
    assert 'baseUrl' not in result['cadProvider']
    assert result['cadProvider']['authentication'] == 'cli-managed'


def test_reading_engine_switch_invalidates_old_candidates():
    local = LocalProvider.provider_info
    assert not same_reading_engine({'provider': {'model': 'gpt-5.6-sol'}}, local)
    assert not same_reading_engine({}, local)
    assert same_reading_engine({'provider': dict(local)}, local)
    assert not same_reading_engine({'provider': dict(local)}, {'mode': 'remote', 'model': 'gpt-6-astra'})
    assert not same_reading_engine({'provider': dict(local)}, {**local, 'model': 'another-model'})
    assert same_reading_engine({'provider': {'model': 'old'}}, {'mode': 'remote', 'model': 'old'})


def test_switching_engines_rereads_source_before_using_saved_draft(tmp_path):
    from tests.test_cad_agent import Provider, Executor, StubSpatialInterpreter, context, execute, finish, raster, run
    from tests.test_cad_agent_continuation_boundaries import Reading, drawing_record
    old_reader = Reading(30)
    files = [raster()]
    old = run(Provider([drawing_record(), execute(), finish()]), Executor(), tmp_path / 'old',
              source_reader=old_reader, files=files)
    state = old['state']
    state['sourceTranscription']['provider'] = {'mode': 'remote', 'model': 'gpt-5.6-sol'}
    state['sourceQuestionReviews'] = [{'scope': 'source_question_reread', 'candidateEvidence': True}]

    def next_action(body):
        value = context(body)
        assert value['currentPlan'] is not None
        assert value['sourceObservations'] == []
        assert value['sourceTranscription']['annotations'][0]['text'] == '60'
        assert body['model'] == 'gpt-6-astra'
        return {'action': 'ask_user', 'message': 'Unclear feature.', 'questions': ['Which feature is intended?']}

    provider = Provider([next_action])
    provider.provider_info = dict(LocalProvider.provider_info)
    fresh = Reading(60)
    result = run(provider, Executor(), tmp_path / 'new', state=state, files=files,
                 source_reader=fresh, spatial_interpreter=StubSpatialInterpreter(), max_turns=1)
    assert len(fresh.calls) == 1
    assert result['sourceQuestionReviews'] == []
    assert result['provider']['model'] == 'gpt-6-astra'
    assert any(item.get('reason') == 'reading_engine_changed' for item in result['trace'])


def test_codex_diagnostics_retain_only_safe_event_categories():
    from app.cad_source_reader import _safe_reader_diagnostics
    assert _safe_reader_diagnostics({'codexEventType': 'item.completed', 'codexItemType': 'error', 'stderr': 'private'}) == {
        'codexEventType': 'item.completed', 'codexItemType': 'error'}
    assert _safe_reader_diagnostics({'codexEventType': 'secret event', 'codexItemType': 'sensitive command'}) == {}
