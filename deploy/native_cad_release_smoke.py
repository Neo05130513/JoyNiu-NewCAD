#!/usr/bin/env python3
"""Isolated native 2D acceptance, extending the existing candidate smoke."""
import sys
from pathlib import Path
import currentcad_release_smoke as baseline

if Path('/opt/joyniu-api/app').is_dir():
    sys.path.insert(0, '/opt/joyniu-api')

original_native = baseline.native_smoke


def native_smoke(root, owner):
    from app.native_drawing import NativeDrawingStore, _read
    drawing, report, dwg = original_native(root, owner)
    store = NativeDrawingStore(root / 'native-workspace.sqlite3')
    doc = store.create(owner, 'Native workspace acceptance')
    doc = store.operate(owner, doc['id'], doc['revision'], [
        {'op': 'add', 'entity': {'type': 'ELLIPSE', 'center': [20, 20], 'majorAxis': [10, 0], 'ratio': .5}},
        {'op': 'add', 'entity': {'type': 'LINE', 'start': [0, 0], 'end': [50, 0]}},
    ])
    ids = [e['id'] for e in doc['entities']]
    doc = store.operate(owner, doc['id'], doc['revision'], [
        {'op': 'group', 'ids': ids}, {'op': 'layout', 'name': 'Layout2'},
    ])
    reopened = NativeDrawingStore(store.database).get(owner, doc['id'])
    baseline.require(reopened['groups'] == doc['groups'], 'Native group reopen mismatch')
    raw, _, _ = store.export(owner, doc['id'])
    actual = _read(raw)
    baseline.require(actual.modelspace().query('ELLIPSE')[0].dxf.ratio == .5
                     and len(actual.groups) == 1
                     and len(actual.layouts.get('Layout2').query('VIEWPORT')) == 2,
                     'Native ellipse/group/layout DXF readback mismatch')
    svg, _, _ = store.export(owner, doc['id'], 'svg', layout='Layout2')
    baseline.require(b'<svg' in svg, 'Native layout SVG missing')
    undone = store.operate(owner, doc['id'], doc['revision'], history='undo')
    baseline.require(not undone['groups'] and 'Layout2' not in undone['layouts'], 'Native undo mismatch')
    redone = store.operate(owner, doc['id'], undone['revision'], history='redo')
    baseline.require(redone['groups'] == doc['groups'] and redone['layouts'] == doc['layouts'], 'Native redo mismatch')
    report['workspace'] = dict.fromkeys(('ellipse', 'groups', 'layoutViewport', 'dxfReadback', 'svgPreview', 'reopen', 'undoRedo'), True)
    return drawing, report, dwg


baseline.native_smoke = native_smoke
if __name__ == '__main__':
    raise SystemExit(baseline.main())
