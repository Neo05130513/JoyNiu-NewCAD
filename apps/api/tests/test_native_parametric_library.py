import copy
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from app.native_drawing import DrawingError, DrawingNotFound, NativeDrawingStore, _read


@pytest.fixture
def library(tmp_path):
    if not shutil.which('node'):
        pytest.skip('Node.js is needed for the real frontend constraint solver contract')
    script = """
import {createNativeTemplate, prepareNativeTemplate} from './src/nativeParametricLibrary.js';
const datum = {id:'datum', type:'CIRCLE', center:[0,0,0], radius:1};
const entities=[datum,{id:'line',type:'LINE',start:[0,0,0],end:[10,0,0]},{id:'circle',type:'CIRCLE',center:[0,0,0],radius:5}];
const constraints=[{type:'fixed',entities:['datum'],geometry:datum},{type:'coincident',entities:['line','datum'],ends:[0,0]},{type:'horizontal',entities:['line']},{type:'length',entities:['line'],value:'width'},{type:'concentric',entities:['datum','circle']},{type:'radius',entities:['circle'],value:'diameter / 2'}];
const template=createNativeTemplate({id:'line-circle',name:'真实参数构件',entities,constraints,parameters:{width:10,diameter:10}});
process.stdout.write(JSON.stringify({template,operation:prepareNativeTemplate(template,{width:45,diameter:6},[100,200]),replacement:prepareNativeTemplate(template,{width:25,diameter:16},[10,20])}));
"""
    result = subprocess.run(['node', '--input-type=module'], input=script, text=True, capture_output=True, cwd=Path(__file__).resolve().parents[3], timeout=20, check=True)
    fixture = json.loads(result.stdout)
    store = NativeDrawingStore(tmp_path / 'library.sqlite')
    created = store.create('alice', '参数图库')
    doc = store.operate('alice', created['id'], 1, [{'op': 'metadata', 'customProperties': {'library': [fixture['template']], 'parameters': {'width': 99}}}])
    return store, doc, fixture


def insert(library):
    store, doc, fixture = library
    return store.operate('alice', doc['id'], doc['revision'], [fixture['operation']])


def test_real_solver_geometry_and_parameter_formula_remap_commit_together_and_reopen(library):
    store, doc, fixture = library
    saved = insert(library)
    instance = saved['customProperties']['libraryInstances'][0]
    assert len(saved['entities']) == 3
    assert len(saved['customProperties']['constraints']) == 6
    assert saved['customProperties']['parameters']['width'] == 99
    assert set(instance['entityMap']) == {'e0', 'e1', 'e2'}
    assert set(instance['entityIds']) == {entity['id'] for entity in saved['entities']}
    assert all(set(constraint['entities']) <= set(instance['entityIds']) for constraint in saved['customProperties']['constraints'])
    radius_constraint = saved['customProperties']['constraints'][-1]
    assert radius_constraint['value'] == instance['parameterMap']['diameter'] + ' / 2'
    output = _read(store.export('alice', doc['id'], 'dxf')[0])
    line = output.modelspace().query('LINE')[0]
    assert tuple(line.dxf.start) == pytest.approx([100, 200, 0], abs=1e-4)
    assert tuple(line.dxf.end) == pytest.approx([145, 200, 0], abs=1e-4)
    assert sorted(entity.dxf.radius for entity in output.modelspace().query('CIRCLE')) == pytest.approx([1, 3])
    reopened = NativeDrawingStore(store.database).get('alice', doc['id'])
    assert reopened['customProperties']['libraryInstances'] == saved['customProperties']['libraryInstances']
    second = store.operate('alice', doc['id'], saved['revision'], [fixture['operation']])
    first_instance, second_instance = second['customProperties']['libraryInstances']
    assert set(first_instance['entityIds']).isdisjoint(second_instance['entityIds'])
    assert set(first_instance['parameterMap'].values()).isdisjoint(second_instance['parameterMap'].values())
    with pytest.raises(DrawingNotFound):
        store.operate('bob', doc['id'], second['revision'], [fixture['operation']])


def test_edit_existing_instance_parameters_replaces_geometry_and_constraints_atomically_with_undo(library):
    store, doc, fixture = library
    saved = insert(library)
    first = saved['customProperties']['libraryInstances'][0]
    updated = store.operate('alice', doc['id'], saved['revision'], [{**fixture['replacement'], 'instanceId': first['id']}])
    current = updated['customProperties']['libraryInstances'][0]
    assert current['id'] == first['id']
    assert len(updated['entities']) == 3
    assert len(updated['customProperties']['constraints']) == 6
    assert set(current['entityIds']).isdisjoint(first['entityIds'])
    line = next(entity for entity in updated['entities'] if entity['type'] == 'LINE')
    assert line['start'] == pytest.approx([10, 20, 0], abs=1e-4)
    assert line['end'] == pytest.approx([35, 20, 0], abs=1e-4)
    assert current['parameters'] == {'width': 25, 'diameter': 16}
    restored = store.operate('alice', doc['id'], updated['revision'], history='undo')
    assert restored['entities'] == saved['entities']
    assert restored['customProperties'] == saved['customProperties']


@pytest.mark.parametrize('mutation', ['external', 'unsolved', 'duplicate', 'unknown_template'])
def test_invalid_template_geometry_or_reference_has_no_partial_insertion(library, mutation):
    store, doc, fixture = library
    operation = copy.deepcopy(fixture['operation'])
    if mutation == 'external':
        operation['constraints'][0]['entities'] = ['outside']
    elif mutation == 'unsolved':
        operation['parameters']['width'] = 100
    elif mutation == 'duplicate':
        operation['entities'][1]['id'] = operation['entities'][0]['id']
    else:
        operation['templateId'] = 'unknown'
    with pytest.raises(DrawingError):
        store.operate('alice', doc['id'], doc['revision'], [operation])
    current = store.get('alice', doc['id'])
    assert current['revision'] == doc['revision']
    assert current['entities'] == []
    assert current['customProperties'] == doc['customProperties']


def test_instance_rebuild_rejects_new_external_constraints_and_associated_dimensions(library):
    store, doc, fixture = library
    saved = insert(library)
    instance = saved['customProperties']['libraryInstances'][0]
    replacement = {**fixture['replacement'], 'instanceId': instance['id']}
    properties = copy.deepcopy(saved['customProperties'])
    properties['constraints'].append({'id': 'outside', 'type': 'length', 'entities': [instance['entityMap']['e1']], 'value': 45})
    linked = store.operate('alice', doc['id'], saved['revision'], [{'op': 'metadata', 'customProperties': properties}])
    with pytest.raises(DrawingError, match='外部约束'):
        store.operate('alice', doc['id'], linked['revision'], [replacement])
    detached = store.operate('alice', doc['id'], linked['revision'], history='undo')
    linked = store.operate('alice', doc['id'], detached['revision'], [{'op': 'dimension', 'kind': 'radius', 'center': [100, 200], 'radius': 3, 'sourceRefs': {'circle': {'entityId': instance['entityMap']['e2']}}}])
    with pytest.raises(DrawingError, match='尺寸标注'):
        store.operate('alice', doc['id'], linked['revision'], [replacement])
    assert store.get('alice', doc['id'])['revision'] == linked['revision']


def test_nonuniform_rectangle_instance_uses_real_solver_then_dxf_measurements(library):
    store, doc, _ = library
    script = """
import {createNativeTemplate, prepareNativeTemplate} from './src/nativeParametricLibrary.js';
const line=(id,start,end)=>({id,type:'LINE',start:[...start,0],end:[...end,0]});
const entities=[line('a',[0,0],[10,0]),line('b',[10,0],[10,20]),line('c',[10,20],[0,20]),line('d',[0,20],[0,0])];
const constraints=entities.map((e,i)=>({type:i%2?'vertical':'horizontal',entities:[e.id]}));
constraints.push({type:'length',entities:['a'],value:'width'},{type:'length',entities:['b'],value:'height'});
for(let i=0;i<4;i++)constraints.push({type:'coincident',entities:[entities[i].id,entities[(i+1)%4].id],ends:[1,0]});
const template=createNativeTemplate({id:'rectangle',name:'非等比矩形',entities,constraints,parameters:{width:10,height:20}});
process.stdout.write(JSON.stringify({template,operation:prepareNativeTemplate(template,{width:40,height:8},[30,60])}));
"""
    result = subprocess.run(['node', '--input-type=module'], input=script, text=True, capture_output=True, cwd=Path(__file__).resolve().parents[3], timeout=20, check=True)
    fixture = json.loads(result.stdout)
    saved = store.operate('alice', doc['id'], doc['revision'], [{'op': 'metadata', 'customProperties': {'library': [fixture['template']]}}, fixture['operation']])
    output = _read(store.export('alice', doc['id'], 'dxf')[0])
    lines = list(output.modelspace().query('LINE'))
    assert sorted((line.dxf.end - line.dxf.start).magnitude for line in lines) == pytest.approx([8, 8, 40, 40], abs=1e-4)
    for index, line in enumerate(lines):
        assert tuple(line.dxf.end) == pytest.approx(tuple(lines[(index + 1) % 4].dxf.start), abs=1e-4)
    assert saved['customProperties']['libraryInstances'][0]['parameters'] == {'width': 40, 'height': 8}
