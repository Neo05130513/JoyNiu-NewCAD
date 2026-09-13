#!/usr/bin/env python3
"""Real candidate-image smoke, using only synthetic users and geometry.

Run inside the candidate (not on a live API):
 docker run --rm --network none --read-only --user 10001:10001 \
   --tmpfs /tmp:rw,exec,nosuid,size=1g,uid=10001,gid=10001 \
   -v "$PWD/deploy/currentcad_release_smoke.py:/smoke.py:ro" \
   -v "/private/new-evidence:/evidence" --entrypoint python CANDIDATE \
   /smoke.py --output-dir /evidence

Optional --restore-data /restore/data copies a read-only mounted, already
verified backup's data into private /tmp before startup. It never opens a live
database for writing. Original rows and files are checked after migration;
only rate-limit rows and transient feature previews already expired at smoke
start may be cleaned up; saved feature revisions remain fully protected.
No AI, external requests, existing customer passwords or Codex login are used.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
from importlib.metadata import version
import io
import json
import math
import os
import platform
from pathlib import Path
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import uuid
import zipfile

# A mounted /smoke.py must load the candidate's freshly copied app, even when
# the base also has an older site-packages installation of the same project.
if Path('/opt/joyniu-api/app').is_dir():
    sys.path.insert(0, '/opt/joyniu-api')


PROTECTED_ROUTES = ('/api/cad/drawings', '/api/cad/features', '/api/cad/designs', '/api/cad/deliveries')


class SmokeCheckFailed(RuntimeError):
    """Only fixed, non-secret diagnostic messages are put in the report."""


def require(value, message):
    if not value:
        raise SmokeCheckFailed(message)


def close(actual, expected, tolerance=1e-6):
    require(math.isfinite(actual) and abs(actual - expected) <= tolerance, 'Geometry measurement mismatch')


def file_hashes(root):
    result = {}
    for path in root.rglob('*'):
        require(not path.is_symlink(), 'Restore data must not contain symlinks')
        if path.is_file():
            result[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


PREVIEW_CACHE_TTL_SECONDS = 1800
PREVIEW_CACHE_DATABASE = 'cad-feature-workspace/features.sqlite3'


def snapshot_database(path, columns=None, *, expired_auth_rate_limit_before=None,
                      expired_manual_preview_before=None):
    with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as database:
        require(database.execute('PRAGMA quick_check').fetchone()[0] == 'ok', 'SQLite integrity failed')
        tables = columns or {row[0]: None for row in database.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        result = {}
        for table, names in tables.items():
            quoted = '"' + table.replace('"', '""') + '"'
            names = names or [row[1] for row in database.execute(f'PRAGMA table_info({quoted})')]
            select = ','.join('"' + name.replace('"', '""') + '"' for name in names)
            hashes, expired, key_hashes = Counter(), {}, set()
            expired_previews, preview_ids = {}, set()
            rate_limits = table == 'auth_rate_limits' and {'key_hash', 'expires_at'}.issubset(names)
            previews = table == 'manual_feature_previews' and {'id', 'owner', 'created', 'payload'}.issubset(names)
            for row in database.execute(f'SELECT {select} FROM {quoted}'):
                row_hash = hashlib.sha256(repr(row).encode()).hexdigest()
                hashes[row_hash] += 1
                if rate_limits:
                    key_hash = hashlib.sha256(repr(row[names.index('key_hash')]).encode()).hexdigest()
                    key_hashes.add(key_hash)
                    expiry = row[names.index('expires_at')]
                    if (expired_auth_rate_limit_before is not None and type(expiry) is int
                            and expiry <= expired_auth_rate_limit_before):
                        expired[row_hash] = key_hash
                if previews:
                    identity = row[names.index('id')]
                    identity_hash = hashlib.sha256(repr(identity).encode()).hexdigest()
                    preview_ids.add(identity_hash)
                    created = row[names.index('created')]
                    if (expired_manual_preview_before is not None
                            and isinstance(identity, str) and re.fullmatch(r'preview_[a-f0-9]{32}', identity)
                            and type(created) in (int, float) and math.isfinite(created)
                            and created < expired_manual_preview_before - PREVIEW_CACHE_TTL_SECONDS):
                        expired_previews[row_hash] = identity_hash
            result[table] = {'columns': names, 'hashes': hashes}
            if rate_limits:
                result[table].update(expiredAuthRateLimitRows=expired, authRateLimitKeyHashes=key_hashes)
            if previews:
                result[table].update(expiredManualPreviewRows=expired_previews, manualPreviewIdHashes=preview_ids)
        return result


def verify_restored_database(before, after):
    """Only exact preexpired cache rows may disappear; no identity may be reused."""
    counts = {'expiredAuthRateLimitRowsAllowed': 0, 'expiredAuthRateLimitRowsRemoved': 0,
              'expiredManualPreviewRowsAllowed': 0, 'expiredManualPreviewRowsRemoved': 0}
    for name, data in before.items():
        require(name in after, 'An original restored database table disappeared')
        missing = data['hashes'] - after[name]['hashes']
        if name == 'auth_rate_limits':
            allowed, identities, category = data.get('expiredAuthRateLimitRows', {}), after[name].get('authRateLimitKeyHashes', set()), 'expiredAuthRateLimitRows'
        elif name == 'manual_feature_previews':
            allowed, identities, category = data.get('expiredManualPreviewRows', {}), after[name].get('manualPreviewIdHashes', set()), 'expiredManualPreviewRows'
        else:
            allowed, identities, category = {}, set(), None
        if category:
            counts[category + 'Allowed'] += len(allowed)
        require(not (missing - Counter(allowed.keys())),
                'An original restored database row changed or disappeared')
        # Cleanup can delete a baseline-expired row, never replace its content
        # or reuse its original identity (even if a replacement is also old).
        require(all(allowed[row_hash] not in identities for row_hash in missing),
                'An original restored database row changed or disappeared')
        if category:
            counts[category + 'Removed'] += sum(missing.values())
    return counts


def native_smoke(root, owner):
    import ezdxf
    from app.native_drawing import NativeDrawingStore, _read, _write, _convert_dwg
    from app.native_dwg import _drawing_signature, _first_difference
    from app.native_drawing_bubbles import APPID
    store = NativeDrawingStore(root / 'joyniu.sqlite3')
    doc = ezdxf.new('R2010', setup=True)
    doc.units = 4
    doc.layers.new('VIEWPORTS')
    doc.layers.new('机械轮廓', dxfattribs={'color': 3, 'lineweight': 35, 'linetype': 'DASHED'})
    block = doc.blocks.new('测试零件', base_point=(1, 2, 0))
    block.add_line((1, 2, 0), (11, 12, 3), dxfattribs={'layer': '机械轮廓'})
    block.add_circle((2, 2), 3)
    doc.modelspace().add_blockref('测试零件', (30, 30), dxfattribs={'rotation': 30, 'xscale': 2, 'yscale': 1.5, 'zscale': 3})
    for layout in (doc.layouts.get('Layout1'), doc.layouts.new('中文第二张')):
        layout.page_setup(size=(297, 210), margins=(10, 10, 10, 10))
        layout.add_text('技术要求：尺寸公差', dxfattribs={'insert': (15, 30)})
        layout.add_viewport(center=(100, 100), size=(100, 80), view_center_point=(20, 20), view_height=50)
    drawing = store.create(owner, '候选镜像检验图', _write(doc), 'fixture.dxf')
    def operate(ops):
        nonlocal drawing
        drawing = store.operate(owner, drawing['id'], drawing['revision'], ops)
    operate([{'op': 'add', 'entity': {'type': 'LINE', 'start': [0, 0], 'end': [150, 0]}}])
    line = next(item['id'] for item in drawing['entities'] if item['type'] == 'LINE')
    operate([{'op': 'dimension', 'kind': 'linear', 'base': [0, 15], 'toleranceUpper': .2, 'toleranceLower': .1,
              'sourceRefs': {'p1': {'entityId': line, 'point': 'start'}, 'p2': {'entityId': line, 'point': 'end'}}}])
    dimension_id = drawing['dimensions'][0]['id']
    operate([{'op': 'bubble', 'dimensionId': dimension_id, 'number': 7, 'position': [-25, 40], 'radius': 4}])
    source = _read(store.export(owner, drawing['id'], 'dxf')[0])
    payload, _, _ = store.export(owner, drawing['id'], 'dwg')
    require(payload.startswith(b'AC1015'), 'Output is not the expected real binary DWG')
    actual = _read(_convert_dwg(payload))
    require(not _first_difference(_drawing_signature(source), _drawing_signature(actual)), 'DWG round trip changed drawing')
    dim = actual.entitydb[dimension_id]
    close(dim.get_measurement(), 150)
    close(dim.override().get('dimtp'), .2)
    close(dim.override().get('dimtm'), .1)
    require(actual.entitydb[drawing['bubbles'][0]['circleId']].has_xdata(APPID), 'Bubble XDATA missing')
    reopened = store.create(owner, 'DWG回读编辑', payload, 'fixture.dwg')
    edited = store.operate(owner, reopened['id'], 1, [{'op': 'update', 'id': line, 'changes': {'end': [160, 0]}}])
    close(next(item['value'] for item in edited['dimensions'] if item['id'] == dimension_id), 160)
    require(edited['bubbles'][0]['number'] == 7, 'Balloon association lost')
    return drawing, {'binaryDwg': True, 'fullDrawingSignatureMatch': True, 'dimensionsRemainNative': True,
                     'measurementMm': 150, 'editedLinkedMeasurementMm': 160, 'toleranceUpper': .2,
                     'toleranceLower': .1, 'bubbleNumber': 7, 'blocksAndPaperLayoutsPreserved': True}, payload


def engineering_smoke(root, owner, output):
    import cadquery as cq
    import ezdxf
    import pymupdf as fitz
    from app.cad_design_workspace import CadDesignWorkspace, _load
    from app.cad_feature_workspace import CadFeatureWorkspace
    designs = CadDesignWorkspace(root / 'cad-designs')
    features = CadFeatureWorkspace(root / 'cad-feature-workspace', design_store=designs)
    plan = {'version': 'cad-plan-v1', 'units': 'mm', 'name': '发布工程图', 'parameters': {},
            'features': [{'id': 'body', 'op': 'box', 'size': [40.5, 20, 10], 'origin': [0, 0, 0]}], 'result': 'body'}
    saved = features.save(owner, {'name': '发布工程图', 'plan': plan, 'changeNote': '独立候选验收'})
    built = features.build(owner, saved['id'], saved['revision'])
    require(built['status'] == 'built', 'Feature worker did not build a solid')
    item = built['sharedDesign']
    step, _ = designs.artifact(owner, item['id'], 'step')
    shape = _load(step)
    require(shape.isValid() and len(shape.Solids()) == 1, 'STEP is not one valid OCCT solid')
    close(shape.Volume(), 8100)
    for actual, expected in zip(item['metrics']['size'], (40.5, 20, 10)):
        close(actual, expected)
    shutil.copyfile(step, output / 'fixture.step')
    vertices = [point for point in item['topology'] if point['kind'] == 'vertex']
    first = vertices[0]
    second = next(point for point in vertices if point['center'][:2] == first['center'][:2] and point['center'][2] != first['center'][2])
    ref = lambda point: {'kind': point['kind'], 'index': point['index']}
    result = designs.drawing(owner, item['id'], {'page': 'A3', 'views': [{'view': 'front'}, {'view': 'right'}],
        'dimensions': [{'a': ref(first), 'b': ref(second), 'orientation': 'vertical', 'viewIndex': 0, 'offset': 30}]})
    close(result['lastDrawing']['dimensions'][0]['value'], 10)
    for key in result['lastDrawing']['artifacts']:
        path, _ = designs.artifact(owner, item['id'], key)
        shutil.copyfile(path, output / ('fixture' + path.suffix))
    dxf = ezdxf.readfile(output / 'fixture.dxf')
    require(not dxf.audit().errors, 'Engineering DXF audit failed')
    close(dxf.modelspace().query('DIMENSION')[0].get_measurement(), 10)
    with fitz.open(output / 'fixture.pdf') as pdf:
        require(len(pdf) == 1, 'Engineering PDF page missing')
        close(pdf[0].rect.width * 25.4 / 72, 420, .001)
        close(pdf[0].rect.height * 25.4 / 72, 297, .001)
        require('工程图' in pdf[0].get_text() and '单位 mm' in pdf[0].get_text(), 'Chinese PDF text missing')
        fonts = pdf.get_page_fonts(0)
        require(bool(fonts) and all(pdf.extract_font(font[0])[-1] for font in fonts), 'PDF font is not embedded')
        glyphs = {chr(char[0]): char for span in pdf[0].get_texttrace() for char in span['chars']}
        for char in '工程图单位实体投影曲线离散':
            require(char in glyphs and glyphs[char][1] > 0, 'PDF Chinese glyph missing')
            pixmap = pdf[0].get_pixmap(clip=fitz.Rect(glyphs[char][3]), colorspace=fitz.csGRAY)
            require(min(pixmap.samples) < 128, 'PDF glyph has no visible strokes')
        pdf[0].get_pixmap().save(output / 'fixture-pdf.png')
        embedded_bytes = [len(pdf.extract_font(font[0])[-1]) for font in fonts]
    return {'featureWorker': True, 'stepValid': True, 'solidCount': 1, 'sizeMm': [40.5, 20, 10],
            'volumeMm3': 8100, 'dxfDimensionMm': 10, 'pdfPaperMm': [420, 297],
            'pdfChineseGlyphsRendered': True, 'pdfEmbeddedFontBytes': embedded_bytes}


def request(base, path, expected=200, payload=None, token=None):
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    req = Request(base + path, data=None if payload is None else json.dumps(payload).encode(), headers=headers)
    try:
        response = urlopen(req, timeout=30)
    except HTTPError as exc:
        response = exc
    with response:
        require(response.status == expected, f'Unexpected HTTP status for {path.split("/")[:4]}')
        raw = response.read(32 * 1024 * 1024 + 1)
        require(len(raw) <= 32 * 1024 * 1024, 'HTTP payload too large')
        return raw


def http_smoke(root, username, password, drawing):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    base = f'http://127.0.0.1:{port}'
    with tempfile.TemporaryFile() as log:
        server = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1',
                                   '--port', str(port), '--no-access-log'], stdout=log, stderr=log)
        try:
            for _ in range(120):
                require(server.poll() is None, 'Candidate API process exited during startup')
                try:
                    health = json.loads(request(base, '/api/v1/health'))
                    break
                except (URLError, OSError):
                    time.sleep(.5)
            else:
                raise SmokeCheckFailed('Candidate API did not become ready')
            require(health.get('geometry', {}).get('available') is True, 'OCCT health unavailable')
            for path in PROTECTED_ROUTES:
                request(base, path, expected=401)
            capabilities = json.loads(request(base, '/api/v1/auth/account-capabilities'))
            require(isinstance(capabilities, dict), 'Platform capabilities unavailable')
            session = json.loads(request(base, '/api/v1/auth/login', payload={'email': username, 'password': password}))
            token = session['access_token']
            for path in PROTECTED_ROUTES:
                result = json.loads(request(base, path, token=token))
                require(isinstance(result.get('items'), list), 'Authenticated workspace list invalid')
            delivery = json.loads(request(base, '/api/cad/deliveries', expected=201, token=token,
                payload={'requestId': str(uuid.uuid4()), 'title': '镜像交付包验收', 'notes': '',
                         'sourceRefs': [{'kind': 'native', 'id': drawing['id'], 'revision': drawing['revision']}]}))
            archive = request(base, f"/api/cad/deliveries/{delivery['id']}/archive", token=token)
            with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
                names = bundle.namelist()
                require(any(name.endswith('.dxf') for name in names), 'Delivery archive has no DXF')
                require(any(name.endswith('.json') for name in names), 'Delivery archive has no metadata')
                require(bundle.testzip() is None, 'Delivery ZIP integrity failed')
                from app.native_drawing import _read
                frozen = _read(bundle.read(next(name for name in names if name.endswith('.dxf'))))
                close(frozen.modelspace().query('DIMENSION')[0].get_measurement(), 150)
            return {'anonymousStatus': {path: 401 for path in PROTECTED_ROUTES},
                    'syntheticLogin': True, 'authenticatedLists': True, 'deliveryArchiveVerified': True}
        finally:
            server.terminate()
            try:
                server.wait(timeout=20)
            except subprocess.TimeoutExpired:
                server.kill(); server.wait(timeout=5)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--restore-data', type=Path, help='Read-only mounted data directory from a verified backup')
    parser.add_argument('--allow-host-runtime', action='store_true', help='Local development check only; disables no-compiler assertion')
    args = parser.parse_args(argv)
    smoke_started_at = int(time.time())
    require(not args.output_dir.exists() or not any(args.output_dir.iterdir()), 'Output directory must be new or empty')
    args.output_dir.mkdir(parents=True, exist_ok=True)
    report = {'ok': False, 'networkCalls': 'loopback only', 'usesCustomerCredentials': False,
              'candidateRuntime': not args.allow_host_runtime, 'architecture': platform.machine()}
    stage = 'runtime'
    try:
        compilers = [name for name in ('cc', 'gcc', 'clang') if shutil.which(name)]
        require(args.allow_host_runtime or not compilers, 'Candidate contains a compiler')
        report['compilerAbsent'] = not compilers
        report['packages'] = {name: version(name) for name in ('cadquery', 'ezdxf', 'PyMuPDF', 'cryptography')}
        report['tools'] = {}
        for name in ('dwgread', 'dwg2dxf', 'dxf2dwg', 'joyniu-native-dwg-adapter'):
            binary = shutil.which(name)
            if binary is None and name == 'joyniu-native-dwg-adapter' and args.allow_host_runtime:
                from app.native_dwg import _adapter
                binary = _adapter(shutil.which('dxf2dwg'))
            require(binary is not None, 'Required native DWG binary missing')
            proc = subprocess.run([binary, '--version'], text=True, capture_output=True, check=True, timeout=15)
            result = proc.stdout + proc.stderr
            require('0.14' in result if name != 'joyniu-native-dwg-adapter' else 'JoyNiu native DWG compatibility adapter' in result,
                    'Unexpected DWG tool version')
            report['tools'][name] = {'sha256': hashlib.sha256(Path(binary).read_bytes()).hexdigest(), 'versionChecked': True}
            if platform.system() == 'Linux':
                dependencies = subprocess.run(['ldd', binary], text=True, capture_output=True, check=True, timeout=15).stdout
                require('not found' not in dependencies and 'libredwg.so' not in dependencies,
                        'DWG executable requires an unavailable/shared LibreDWG runtime')
                report['tools'][name]['staticLibreDwgLinkVerified'] = True
        with tempfile.TemporaryDirectory(prefix='joyniu-candidate-smoke-') as folder:
            root = Path(folder) / 'data'
            original_hashes = {}
            restored = {}
            if args.restore_data:
                stage = 'restore-copy'
                original_hashes = file_hashes(args.restore_data)
                require('joyniu.sqlite3' in original_hashes, 'Restore source has no platform database')
                shutil.copytree(args.restore_data, root)
                for database in root.rglob('*.sqlite3'):
                    relative = str(database.relative_to(root))
                    restored[relative] = snapshot_database(database,
                        expired_auth_rate_limit_before=smoke_started_at if relative == 'joyniu.sqlite3' else None,
                        expired_manual_preview_before=smoke_started_at if relative == PREVIEW_CACHE_DATABASE else None)
            else:
                root.mkdir()
            os.environ.update({'JOYNIU_DB': str(root / 'joyniu.sqlite3'), 'JOYNIU_CAD_AGENT_DIR': str(root / 'cad-agent'),
                'JOYNIU_CAD_DESIGNS_ROOT': str(root / 'cad-designs'), 'JOYNIU_OPERATIONS_DIR': str(root / 'operations'),
                'JOYNIU_AUTH_SECRET': secrets.token_hex(32), 'JOYNIU_BILLING_ENABLED': 'false',
                'JOYNIU_ONLINE_PAYMENTS_ENABLED': 'false', 'JOYNIU_PUBLIC_REGISTRATION': 'false',
                'JOYNIU_AI_ALLOW_ANONYMOUS': '0', 'JOYNIU_ENV': 'production'})
            from app.platform import AuthService, Role
            username = 'candidate-' + uuid.uuid4().hex + '@example.invalid'
            password = secrets.token_urlsafe(32)
            actor = AuthService(root / 'joyniu.sqlite3').create_user(username, password, '候选验收', roles=[Role.ADMIN])
            stage = 'native-dwg'
            drawing, report['native'], dwg = native_smoke(root, actor.id)
            (args.output_dir / 'fixture.dwg').write_bytes(dwg)
            stage = 'engineering'
            report['engineering'] = engineering_smoke(root, actor.id, args.output_dir)
            stage = 'http'
            report['http'] = http_smoke(root, username, password, drawing)
            if args.restore_data:
                stage = 'restored-data-preservation'
                cleanup = {'expiredAuthRateLimitRowsAllowed': 0, 'expiredAuthRateLimitRowsRemoved': 0,
                           'expiredManualPreviewRowsAllowed': 0, 'expiredManualPreviewRowsRemoved': 0}
                for relative, tables in restored.items():
                    after = snapshot_database(root / relative, {name: data['columns'] for name, data in tables.items()})
                    counts = verify_restored_database(tables, after)
                    for key, value in counts.items():
                        cleanup[key] += value
                require(file_hashes(args.restore_data) == original_hashes, 'Read-only restore source changed')
                report['restore'] = {'sourceUnchanged': True,
                    'originalDatabaseRowsPreserved': cleanup['expiredAuthRateLimitRowsRemoved'] == 0 and cleanup['expiredManualPreviewRowsRemoved'] == 0,
                    'requiredOriginalRowsPreserved': True, 'businessRowsPreserved': True,
                    'databases': len(restored), 'sourceFiles': len(original_hashes), **cleanup,
                    'expiredAuthRateLimitCutoffEpoch': smoke_started_at,
                    'expiredManualPreviewCutoffEpoch': smoke_started_at - PREVIEW_CACHE_TTL_SECONDS,
                    'previewCacheTtlSeconds': PREVIEW_CACHE_TTL_SECONDS,
                    'rowPreservationException': 'Only exact auth_rate_limits rows expired at smoke start and exact manual_feature_previews cache rows created more than 1800 seconds before smoke start may be deleted. Identity reuse, row changes, saved feature revisions and all other business-data changes remain forbidden.'}
            report['ok'] = True
    except Exception as exc:
        report['failure'] = {'stage': stage, 'type': type(exc).__name__}
        if isinstance(exc, SmokeCheckFailed):
            report['failure']['check'] = str(exc)
        # Keep reports safe when using a customer backup: no exception bodies,
        # account details, HTTP payloads, database contents or runtime logs.
    (args.output_dir / 'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
