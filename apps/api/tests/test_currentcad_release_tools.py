"""Private build contexts and restored data are checked without touching Docker."""
import hashlib
import importlib.util
from pathlib import Path
import sqlite3

import pytest


ROOT = Path(__file__).resolve().parents[3]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'deploy' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


builder = load('build_currentcad_tools')
smoke = load('currentcad_release_smoke')


def test_cached_tools_context_keeps_release_hash_and_excludes_private_files(tmp_path):
    archive = tmp_path / 'source.tar.xz'
    archive.write_bytes(b'unit test release archive')
    sha = hashlib.sha256(archive.read_bytes()).hexdigest()
    (tmp_path / 'deploy').mkdir()
    dockerfile = (ROOT / 'deploy/Dockerfile.api').read_text()
    actual_sha = '62ebb73b984f865960f20ed26619ea5f8789d5e3fd088fa40a2598384da81275'
    (tmp_path / 'deploy/Dockerfile.api').write_text(dockerfile.replace(actual_sha, sha))
    target = tmp_path / 'apps/api/app'
    target.mkdir(parents=True)
    (target / 'native_drawing_libredwg.c').write_text('int main() {return 0;}')
    (target / '.env').write_text('SECRET=must-not-copy')
    folder = builder.prepare_context(tmp_path, archive)
    text = (folder / 'Dockerfile').read_text()
    assert 'COPY source.tar.xz ./source.tar.xz' in text
    assert 'sha256sum -c -' in text and sha in text
    assert 'RUN curl' not in text and ' AS api' not in text
    assert (folder / 'source.tar.xz').read_bytes() == archive.read_bytes()
    assert not (folder / 'apps/api/app/.env').exists()


def test_wrong_archive_is_rejected_before_context_creation(tmp_path, monkeypatch):
    archive = tmp_path / 'corrupt.tar.xz'
    archive.write_bytes(b'corrupt')
    monkeypatch.setattr(builder.tempfile, 'mkdtemp', lambda **kwargs: pytest.fail('must reject before copying'))
    with pytest.raises(ValueError, match='SHA-256'):
        builder.prepare_context(ROOT, archive)


def test_requested_architecture_is_asserted_before_package_install():
    folder = builder.prepare_context(ROOT, platform='linux/amd64')
    text = (folder / 'Dockerfile').read_text()
    assert 'FROM --platform=linux/amd64' in text
    assert text.index('dpkg --print-architecture') < text.index('apt-get')
    assert 'make -j1' in text


def test_restore_original_rows_detect_deletion_and_mutation_without_exposing_data(tmp_path):
    database = tmp_path / 'old.sqlite3'
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE customers(id INTEGER, name TEXT)')
        db.executemany('INSERT INTO customers VALUES (?,?)', [(1, 'PRIVATE-ALICE'), (2, 'PRIVATE-BOB')])
    before = smoke.snapshot_database(database)
    assert 'PRIVATE' not in repr(before)
    with sqlite3.connect(database) as db:
        db.execute('ALTER TABLE customers ADD COLUMN additional TEXT')
        db.execute('INSERT INTO customers VALUES (3,?,NULL)', ('new',))
    columns = {name: data['columns'] for name, data in before.items()}
    after = smoke.snapshot_database(database, columns)
    assert not (before['customers']['hashes'] - after['customers']['hashes'])
    with sqlite3.connect(database) as db:
        db.execute("UPDATE customers SET name='changed' WHERE id=1")
    after = smoke.snapshot_database(database, columns)
    assert before['customers']['hashes'] - after['customers']['hashes']


def test_restore_source_symlink_is_rejected(tmp_path):
    (tmp_path / 'link').symlink_to('/tmp')
    with pytest.raises(RuntimeError, match='symlinks'):
        smoke.file_hashes(tmp_path)


@pytest.fixture
def restore_database(tmp_path):
    database = tmp_path / 'joyniu.sqlite3'
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE auth_rate_limits(key_hash TEXT PRIMARY KEY, window_start INTEGER, attempts INTEGER, expires_at INTEGER)')
        db.executemany('INSERT INTO auth_rate_limits VALUES (?,0,1,?)',
                       [('PRIVATE-expired', 99), ('PRIVATE-boundary', 100), ('PRIVATE-active', 101)])
        db.execute('CREATE TABLE billing_accounts(owner_id TEXT PRIMARY KEY, credit_units INTEGER)')
        db.execute('INSERT INTO billing_accounts VALUES (?,2500)', ('PRIVATE-owner',))
        db.execute('CREATE TABLE auth_sessions(id TEXT PRIMARY KEY, expires_at INTEGER)')
        db.execute('INSERT INTO auth_sessions VALUES (?,99)', ('PRIVATE-session',))
    return database


def restored_after(database, before):
    return smoke.snapshot_database(database, {name: data['columns'] for name, data in before.items()})


def test_restore_allows_only_exact_preexpired_rate_limit_deletions(restore_database):
    before = smoke.snapshot_database(restore_database, expired_auth_rate_limit_before=100)
    assert 'PRIVATE' not in repr(before)
    assert smoke.verify_restored_database(before, restored_after(restore_database, before)) == {
        'expiredAuthRateLimitRowsAllowed': 2, 'expiredAuthRateLimitRowsRemoved': 0,
        'expiredManualPreviewRowsAllowed': 0, 'expiredManualPreviewRowsRemoved': 0}
    with sqlite3.connect(restore_database) as db:
        db.execute('DELETE FROM auth_rate_limits WHERE expires_at<=100')
        db.execute('INSERT INTO auth_rate_limits VALUES (?,100,1,400)', ('new-synthetic-login',))
    assert smoke.verify_restored_database(before, restored_after(restore_database, before)) == {
        'expiredAuthRateLimitRowsAllowed': 2, 'expiredAuthRateLimitRowsRemoved': 2,
        'expiredManualPreviewRowsAllowed': 0, 'expiredManualPreviewRowsRemoved': 0}


@pytest.mark.parametrize('change', [
    'DELETE FROM auth_rate_limits WHERE expires_at=101',
    'UPDATE auth_rate_limits SET attempts=2 WHERE expires_at=101',
    'UPDATE auth_rate_limits SET attempts=2 WHERE expires_at=99',
    'UPDATE auth_rate_limits SET expires_at=400 WHERE expires_at=99',
    'UPDATE billing_accounts SET credit_units=0',
    'DELETE FROM billing_accounts',
    'DELETE FROM auth_sessions',
])
def test_restore_rejects_nonexpired_cleanup_and_business_or_session_changes(restore_database, change):
    before = smoke.snapshot_database(restore_database, expired_auth_rate_limit_before=100)
    with sqlite3.connect(restore_database) as db:
        db.execute(change)
    with pytest.raises(smoke.SmokeCheckFailed, match='original restored database row'):
        smoke.verify_restored_database(before, restored_after(restore_database, before))


def test_restore_without_explicit_cutoff_does_not_allow_rate_limit_deletion(restore_database):
    before = smoke.snapshot_database(restore_database)
    with sqlite3.connect(restore_database) as db:
        db.execute('DELETE FROM auth_rate_limits WHERE expires_at=99')
    with pytest.raises(smoke.SmokeCheckFailed, match='original restored database row'):
        smoke.verify_restored_database(before, restored_after(restore_database, before))


@pytest.fixture
def preview_database(tmp_path):
    database=tmp_path/'features.sqlite3'
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE manual_feature_previews(id TEXT PRIMARY KEY, owner TEXT, created REAL, payload TEXT)')
        db.executemany('INSERT INTO manual_feature_previews VALUES (?,?,?,?)',
            [('preview_'+str(i)*32,'PRIVATE-owner',created,'PRIVATE-geometry') for i,created in ((1,2999),(2,3000),(3,3001))])
        db.execute('CREATE TABLE manual_features(id TEXT, revision INTEGER, owner TEXT, payload TEXT, PRIMARY KEY(id,revision))')
        db.execute('INSERT INTO manual_features VALUES (?,?,?,?)',('saved-design',1,'PRIVATE-owner','PRIVATE-saved-version'))
        db.execute('CREATE TABLE manual_feature_commits(owner TEXT, request_id TEXT, digest TEXT)')
        db.execute('INSERT INTO manual_feature_commits VALUES (?,?,?)',('PRIVATE-owner','request1','original'))
    return database


def test_restore_preview_cleanup_uses_exact_1800_second_strict_baseline_cutoff(preview_database):
    assert smoke.PREVIEW_CACHE_TTL_SECONDS==1800
    before=smoke.snapshot_database(preview_database,expired_manual_preview_before=4800)
    assert 'PRIVATE' not in repr(before)
    with sqlite3.connect(preview_database) as db:
        db.execute('DELETE FROM manual_feature_previews WHERE created<3000')
        db.execute('INSERT INTO manual_feature_previews VALUES (?,?,?,?)',('preview_'+'4'*32,'new-owner',4801,'new-geometry'))
    counts=smoke.verify_restored_database(before,restored_after(preview_database,before))
    assert counts=={'expiredAuthRateLimitRowsAllowed':0,'expiredAuthRateLimitRowsRemoved':0,
                    'expiredManualPreviewRowsAllowed':1,'expiredManualPreviewRowsRemoved':1}


@pytest.mark.parametrize('change',[
    'DELETE FROM manual_feature_previews WHERE created=3000',
    'DELETE FROM manual_feature_previews WHERE created=3001',
    "UPDATE manual_feature_previews SET payload='changed' WHERE created=2999",
    "UPDATE manual_feature_previews SET owner='changed' WHERE created=2999",
    'UPDATE manual_feature_previews SET created=2998 WHERE created=2999',
    'DELETE FROM manual_features',
    "UPDATE manual_features SET payload='changed'",
    'DELETE FROM manual_feature_commits',
])
def test_preview_exception_never_permits_row_changes_unexpired_deletion_or_saved_business_mutation(preview_database,change):
    before=smoke.snapshot_database(preview_database,expired_manual_preview_before=4800)
    with sqlite3.connect(preview_database) as db:db.execute(change)
    with pytest.raises(smoke.SmokeCheckFailed,match='original restored database row'):
        smoke.verify_restored_database(before,restored_after(preview_database,before))


@pytest.mark.parametrize('replacement_created',[2998,4801])
def test_deleted_expired_preview_identity_cannot_be_reinserted(preview_database,replacement_created):
    before=smoke.snapshot_database(preview_database,expired_manual_preview_before=4800)
    with sqlite3.connect(preview_database) as db:
        db.execute('DELETE FROM manual_feature_previews WHERE created=2999')
        db.execute('INSERT INTO manual_feature_previews VALUES (?,?,?,?)',('preview_'+'1'*32,'new-owner',replacement_created,'new-payload'))
    with pytest.raises(smoke.SmokeCheckFailed,match='original restored database row'):
        smoke.verify_restored_database(before,restored_after(preview_database,before))


def test_preview_cleanup_without_explicit_baseline_is_not_allowed(preview_database):
    before=smoke.snapshot_database(preview_database)
    with sqlite3.connect(preview_database) as db:db.execute('DELETE FROM manual_feature_previews WHERE created=2999')
    with pytest.raises(smoke.SmokeCheckFailed,match='original restored database row'):
        smoke.verify_restored_database(before,restored_after(preview_database,before))


def test_expired_rate_limit_key_replacement_remains_rejected(restore_database):
    before=smoke.snapshot_database(restore_database,expired_auth_rate_limit_before=100)
    with sqlite3.connect(restore_database) as db:
        db.execute('DELETE FROM auth_rate_limits WHERE expires_at=99')
        db.execute('INSERT INTO auth_rate_limits VALUES (?,100,1,400)',('PRIVATE-expired',))
    with pytest.raises(smoke.SmokeCheckFailed,match='original restored database row'):
        smoke.verify_restored_database(before,restored_after(restore_database,before))
