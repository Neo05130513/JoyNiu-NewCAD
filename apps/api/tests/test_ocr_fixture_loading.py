"""Release archives must not interpret macOS metadata as OCR fixtures."""
import json

import pytest

from app.ocr import OCRService
from app.platform import AuthService
from app.platform_api import build_platform_services


def fixture_files(directory):
    fixture = {'fixture_id': 'visible-test-fixture', 'aliases': ['fixture-alias'], 'verified': False, 'dimensions': []}
    (directory / 'fixture.json').write_text(json.dumps(fixture), encoding='utf-8')
    # AppleDouble starts with a binary header; its resource payload is not JSON.
    (directory / '._fixture.json').write_bytes(b'\x00\x05\x16\x07\x00\x02\x00\x00\xffAppleDouble')
    (directory / '.metadata.json').write_bytes(b'\xff\xfehidden-metadata')
    return fixture


def test_appledouble_and_hidden_metadata_never_enter_fixture_catalog(tmp_path):
    fixture = fixture_files(tmp_path)
    service = OCRService(fixture_directory=tmp_path, enable_live_ocr=False)
    assert service.fixtures == {'visible-test-fixture': fixture, 'fixture-alias': fixture}
    assert len(service.list_fixtures()) == 1


@pytest.mark.parametrize('contents,error', [(b'not-json', json.JSONDecodeError), (b'\xff\xfevisible-corrupt-file', UnicodeDecodeError)])
def test_real_corrupted_fixture_still_raises(tmp_path, contents, error):
    fixture_files(tmp_path)
    (tmp_path / 'corrupted.json').write_bytes(contents)
    with pytest.raises(error):
        OCRService(fixture_directory=tmp_path, enable_live_ocr=False)


def test_existing_platform_database_opens_with_appledouble_fixture_sidecars(tmp_path, monkeypatch):
    fixture_directory = tmp_path / 'fixtures'
    fixture_directory.mkdir()
    fixture_files(fixture_directory)
    database = tmp_path / 'existing.sqlite3'
    secret = 'local-fixture-migration-test-secret'
    auth = AuthService(database, token_secret=secret)
    existing = auth.create_user('fixture-startup@example.test', 'test-only-password', 'Existing admin', roles=['admin'])
    auth.close()
    monkeypatch.setattr('app.ocr._FIXTURE_DIR', fixture_directory)
    services = build_platform_services(database, auth_secret=secret, enable_live_ocr=False)
    try:
        assert services.auth.get_user(existing.id).roles == ('admin',)
        assert services.auth.count_users() == 1
        assert set(services.ocr.fixtures) == {'visible-test-fixture', 'fixture-alias'}
    finally:
        services.close()
