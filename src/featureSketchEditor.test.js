import test, { before, after } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtemp, rm } from 'node:fs/promises'
import { existsSync } from 'node:fs'
import { spawnSync } from 'node:child_process'
import { resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { rolldown } from 'rolldown'
import React from 'react'
import { editableProfile } from './cadEditorTransactions.js'
import { resolveSketchFeature } from './featureSketchExpressions.js'
import { sketchContourList } from './featureSketchContours.js'
import { validateSketchConstraints } from './featureSketchConstraints.js'

let directory, Editor
before(async () => {
  directory = await mkdtemp(resolve('node_modules/.sketch-ui-'))
  const bundle = await rolldown({ input: resolve('src/FeatureSketchEditor.jsx'), external: ['react/jsx-runtime', 'react-dom'], transform: { jsx: { runtime: 'automatic' } }, plugins: [{
    name: 'sketch-ui-hooks',
    resolveId(source) { if (source === 'react') return '\0sketch-hooks' },
    load(id) {
      if (id === '\0sketch-hooks') return { code: ['useState', 'useRef', 'useEffect'].map(name => `export const ${name}=(...args)=>globalThis.__sketchHooks.${name}(...args);`).join('\n'), moduleType: 'js' }
      if (id.endsWith('.css')) return { code: '', moduleType: 'js' }
    },
  }] })
  try { await bundle.write({ file: resolve(directory, 'sketch.mjs'), format: 'esm' }) } finally { await bundle.close() }
  Editor = (await import(pathToFileURL(resolve(directory, 'sketch.mjs')).href)).default
})
after(async () => { if (directory) await rm(directory, { recursive: true, force: true }); delete globalThis.__sketchHooks })

const profile = () => ({ id: 'profile1', op: 'profile_extrude', plane: 'XY', origin: [4, 5, 6], start: [0, 0], segments: [{ type: 'line', to: [20, 0] }, { type: 'line', to: [20, 10] }, { type: 'line', to: [0, 10] }], distance: 5 })
function harness(initial = profile(), extraProps = {}) {
  let slots = [], effects = [], cursor = 0, dirty = true, tree, key
  let props = { feature: structuredClone(initial), disabled: false, ...extraProps }
  const changes = [], parameterChanges = [], captures = [], bounds = { left: 10, top: 20, width: 400, height: 300 }
  const canvas = { getBoundingClientRect: () => bounds, setPointerCapture: id => captures.push(id), releasePointerCapture() {} }
  const cleanup = () => { for (const slot of slots) slot?.cleanup?.() }
  globalThis.__sketchHooks = {
    useState(initial) { const i = cursor++; slots[i] ||= { value: typeof initial === 'function' ? initial() : initial }; return [slots[i].value, value => { const next = typeof value === 'function' ? value(slots[i].value) : value; if (!Object.is(next, slots[i].value)) { slots[i].value = next; dirty = true } }] },
    useRef(initial) { const i = cursor++; return slots[i] ||= { current: initial } },
    useEffect(fn, deps) { const i = cursor++, old = slots[i]; if (!old || deps.some((value, index) => !Object.is(value, old.deps[index]))) { slots[i] = { deps, cleanup: old?.cleanup }; effects.push(() => { slots[i].cleanup?.(); slots[i].cleanup = fn() }) } },
  }
  const nodes = (value = tree) => value?.$$typeof === Symbol.for('react.portal') ? nodes(value.children) : React.isValidElement(value) ? [value, ...React.Children.toArray(value.props.children).flatMap(nodes)] : []
  const inlineNodes = (value = tree) => React.isValidElement(value) ? [value, ...React.Children.toArray(value.props.children).flatMap(inlineNodes)] : []
  const text = value => React.isValidElement(value) ? React.Children.toArray(value.props.children).map(text).join('') : String(value ?? '')
  const find = predicate => { const node = nodes().find(predicate); assert.ok(node, 'expected sketch control'); return node }
  const svg = () => find(node => node.type === 'svg')
  const render = () => {
    let rounds = 0
    do {
      dirty = false
      const wrapper = Editor({ ...props, onChange: (feature, parameters) => { changes.push(structuredClone(feature)); parameterChanges.push(structuredClone(parameters)); props = { ...props, feature, parameters }; dirty = true } })
      if (key !== wrapper.key) { cleanup(); slots = []; effects = []; key = wrapper.key }
      cursor = 0; tree = wrapper.type(wrapper.props)
      svg().props.ref.current = canvas
      while (effects.length) effects.shift()()
      assert.ok(++rounds < 30, 'component should settle')
    } while (dirty)
  }
  const button = name => find(node => node.type === 'button' && text(node) === name)
  const labeled = label => find(node => node.props['aria-label'] === label)
  const event = (node, name, arg) => { node.props[name](arg); render() }
  const input = (label, value) => event(labeled(label), 'onChange', { target: { value } })
  const click = name => event(button(name), 'onClick')
  const pointer = (x, y, pointerId = 1) => {
    const view = svg().props.viewBox.split(' ').map(Number), scale = Math.min(bounds.width / view[2], bounds.height / view[3])
    return { clientX: bounds.left + (bounds.width - view[2] * scale) / 2 + (x - view[0]) * scale, clientY: bounds.top + (bounds.height - view[3] * scale) / 2 + (-y - view[1]) * scale, pointerId, button: 0, stopPropagation() {}, preventDefault() {} }
  }
  const selectPoint = (label = '起点') => event(find(node => node.type === 'circle' && node.props['aria-label']?.startsWith(`${label} `)), 'onKeyDown', { key: 'Enter', preventDefault() {} })
  const portals = (value = tree) => value?.$$typeof === Symbol.for('react.portal') ? [value] : React.isValidElement(value) ? React.Children.toArray(value.props.children).flatMap(portals) : []
  const update = patch => { props = { ...props, ...patch }; render() }
  render()
  return { render, find, nodes, inlineNodes, portals, selectPoint, bounds, text: () => text(tree), button, labeled, event, input, click, pointer, svg, update, changes, parameterChanges, captures, feature: () => props.feature, parameters: () => props.parameters, unmount: cleanup }
}

test('dragging a numeric point previews without changes and commits exactly once at pointer release', () => {
  const ui = harness()
  try {
    const start = ui.find(node => node.type === 'circle' && node.props['aria-label']?.startsWith('起点 '))
    ui.event(start, 'onPointerDown', ui.pointer(0, 0))
    ui.event(ui.svg(), 'onPointerMove', ui.pointer(-2, 3))
    assert.equal(ui.changes.length, 0, 'moving must not flood parent undo history')
    assert.equal(ui.labeled('所选点 X 坐标').props.value, '-2')
    ui.event(ui.svg(), 'onPointerUp', ui.pointer(-2, 3))
    assert.equal(ui.changes.length, 1)
    assert.deepEqual(ui.feature().start, [-2, 3])
    assert.deepEqual(ui.feature().origin, [4, 5, 6])
    assert.equal(ui.feature().distance, 5)
    assert.deepEqual(ui.captures, [1])
  } finally { ui.unmount() }
})

test('drawing a three-point arc takes two clicks, supports line append and explicit edge deletion', () => {
  const ui = harness()
  try {
    ui.click('画三点圆弧')
    ui.event(ui.svg(), 'onPointerDown', ui.pointer(5, 15))
    assert.equal(ui.changes.length, 0)
    assert.match(ui.text(), /再点击圆弧终点/)
    ui.event(ui.svg(), 'onPointerDown', ui.pointer(10, 10))
    assert.deepEqual(ui.feature().segments.at(-1), { type: 'arc', through: [5, 15], to: [10, 10] })
    assert.ok(ui.nodes().some(node => node.type === 'path' && node.props.d.includes(' A ')))
    ui.click('画直线'); ui.event(ui.svg(), 'onPointerDown', ui.pointer(17, 5))
    assert.deepEqual(ui.feature().segments.at(-1), { type: 'line', to: [17, 5] })
    ui.event(ui.labeled('删除草图边 5'), 'onClick')
    assert.equal(ui.feature().segments.length, 4)
    assert.equal(ui.changes.length, 3)
  } finally { ui.unmount() }
})

test('shape replacement is explicit, rejects missing sizes and creates a real closed two-arc circle', () => {
  const ui = harness()
  try {
    ui.selectPoint()
    assert.equal(ui.changes.length, 0)
    ui.click('用矩形替换轮廓')
    assert.equal(ui.changes.length, 0)
    assert.match(ui.text(), /宽度请输入/)
    ui.input('新轮廓 宽度', '12'); ui.input('新轮廓 高度', '7'); ui.click('用矩形替换轮廓')
    assert.deepEqual(ui.feature().segments[1].to, [12, 7])
    ui.input('新建草图形状', 'circle'); ui.input('新轮廓 半径', '4'); ui.click('用圆替换轮廓')
    assert.equal(ui.feature().segments.length, 2)
    assert.ok(ui.feature().segments.every(segment => segment.type === 'arc'))
    assert.deepEqual(ui.feature().start, [4, 0])
    assert.match(ui.text(), /首尾已闭合/)
    assert.equal(ui.labeled('删除草图边 1').props.disabled, true)
    assert.equal(ui.feature().id, 'profile1')
  } finally { ui.unmount() }
})

test('expressions remain visible and unchanged until both numeric coordinates are explicitly applied', () => {
  const ui = harness({ ...profile(), start: ['width / 2', 0] })
  try {
    ui.click('定位未解析点')
    assert.equal(ui.labeled('所选点 X 坐标').props.value, 'width / 2')
    assert.equal(ui.nodes().filter(node => node.type === 'circle' && node.props['aria-label']?.startsWith('起点 ')).length, 0)
    ui.click('应用坐标'); assert.equal(ui.changes.length, 0)
    assert.match(ui.text(), /表达式须在此明确改为数值/)
    ui.input('所选点 X 坐标', '2.75')
    assert.equal(ui.feature().start[0], 'width / 2')
    ui.click('应用坐标')
    assert.deepEqual(ui.feature().start, [2.75, 0])
    assert.equal(ui.changes.length, 1)
  } finally { ui.unmount() }
})

test('pointer cancellation and disabling during drag cannot modify a feature', () => {
  const ui = harness()
  try {
    const start = () => ui.find(node => node.type === 'circle' && node.props['aria-label']?.startsWith('起点 '))
    ui.event(start(), 'onPointerDown', ui.pointer(0, 0)); ui.event(ui.svg(), 'onPointerMove', ui.pointer(1, 3)); ui.event(ui.svg(), 'onPointerCancel', {})
    assert.deepEqual(ui.feature().start, [0, 0]); assert.equal(ui.changes.length, 0)
    ui.event(start(), 'onPointerDown', ui.pointer(0, 0)); ui.event(ui.svg(), 'onPointerMove', ui.pointer(3, 2)); ui.update({ disabled: true })
    ui.event(ui.svg(), 'onPointerUp', ui.pointer(3, 2))
    ui.click('应用坐标')
    assert.equal(ui.changes.length, 0)
    assert.equal(ui.button('画直线').props.disabled, true)
  } finally { ui.unmount() }
})

test('switching feature identity clears pending arc, point selection and new-shape input drafts', () => {
  const ui = harness()
  try {
    ui.selectPoint()
    ui.input('选择草图控制点', '1:to'); ui.input('所选点 X 坐标', '999'); ui.input('新轮廓 宽度', '80')
    ui.click('画三点圆弧'); ui.event(ui.svg(), 'onPointerDown', ui.pointer(5, 15))
    ui.update({ feature: { ...profile(), id: 'profile2', start: [3, 4], plane: 'XZ' } })
    assert.equal(ui.button('选择 / 拖动').props['aria-pressed'], true)
    assert.equal(ui.nodes().some(node => node.type === 'aside'), false)
    ui.selectPoint()
    assert.equal(ui.labeled('选择草图控制点').props.value, 'start')
    assert.equal(ui.labeled('所选点 X 坐标').props.value, '3')
    assert.equal(ui.labeled('所选点 Z 坐标').props.value, '4')
    assert.equal(ui.labeled('新轮廓 宽度').props.value, '')
    assert.doesNotMatch(ui.text(), /已选圆弧经过点/)
    assert.equal(ui.changes.length, 0)
  } finally { ui.unmount() }
})

test('plane and origin edits preserve local sketch data and exact arc inputs preserve three-point values', () => {
  const ui = harness()
  try {
    ui.selectPoint()
    ui.input('草图平面', 'YZ')
    assert.equal(ui.feature().plane, 'YZ')
    assert.deepEqual(ui.feature().start, [0, 0])
    assert.deepEqual(ui.feature().origin, [4, 5, 6])
    assert.equal(ui.labeled('所选点 Y 坐标').props.value, '0')
    ui.input('草图原点 X', '12.25'); ui.click('应用原点')
    assert.deepEqual(ui.feature().origin, [12.25, 5, 6])
    ui.input('精确新边类型', 'arc')
    ui.input('新边 终点 Y', '10.125'); ui.input('新边 终点 Z', '10')
    ui.input('新边 经过点 Y', '5'); ui.input('新边 经过点 Z', '15.75')
    ui.click('添加这条边')
    assert.deepEqual(ui.feature().segments.at(-1), { type: 'arc', to: [10.125, 10], through: [5, 15.75] })
    assert.equal(ui.feature().distance, 5)
  } finally { ui.unmount() }
})

test('empty canvas draws from the chosen first point, and shape tools require two explicit clicks', () => {
  const ui = harness({ ...profile(), start: [0, 0], segments: [] }, { variant: 'canvas' })
  try {
    assert.match(ui.text(), /空草图/)
    assert.equal(ui.svg().props.viewBox, '-50 -50 100 100')
    ui.click('画直线'); ui.event(ui.svg(), 'onPointerDown', ui.pointer(3, 4))
    assert.equal(ui.changes.length, 0)
    ui.event(ui.svg(), 'onPointerDown', ui.pointer(13, 4))
    assert.deepEqual(ui.feature().start, [3, 4]); assert.deepEqual(ui.feature().segments, [{ type: 'line', to: [13, 4] }])
    ui.input('新图形加入方式','replace')
    ui.click('画矩形'); ui.event(ui.svg(), 'onPointerDown', ui.pointer(2, 3))
    assert.equal(ui.changes.length, 1)
    ui.event(ui.svg(), 'onPointerDown', ui.pointer(14, 10))
    assert.deepEqual(ui.feature().start, [2, 3]); assert.deepEqual(ui.feature().segments[1].to, [14, 10])
    ui.click('画圆'); ui.event(ui.svg(), 'onPointerDown', ui.pointer(5, 5)); ui.event(ui.svg(), 'onPointerDown', ui.pointer(8, 5))
    assert.equal(ui.feature().segments.length, 2); assert.deepEqual(ui.feature().start, [8, 5])
  } finally { ui.unmount() }
})

test('a mouse-drawn rectangle has four selectable dimensions and its closing edge can be edited and undone', () => {
  const ui = harness({ ...profile(), start: [0, 0], segments: [] }, { variant: 'canvas' })
  try {
    ui.click('画矩形'); ui.event(ui.svg(), 'onPointerDown', ui.pointer(2, 3)); ui.event(ui.svg(), 'onPointerDown', ui.pointer(14, 10))
    const rectangle = structuredClone(ui.feature())
    assert.equal(rectangle.segments.length, 4)
    assert.deepEqual(rectangle.segments[3].to, rectangle.start)
    assert.equal(ui.nodes().filter(node => node.type === 'path' && node.props['aria-label']?.startsWith('选择草图边')).length, 4)
    for (let edge = 1; edge <= 4; edge++) assert.ok(ui.labeled(`编辑边 ${edge} 长度`))
    assert.match(ui.text(), /首尾已闭合/); assert.doesNotMatch(ui.text(), /虚线为自动闭合边/)
    ui.event(ui.labeled('选择草图边 4'), 'onKeyDown', { key: 'Enter', preventDefault() {} })
    assert.equal(ui.feature().sketchConstraints.find(c => c.type === 'vertical' && c.edge === 3).edge, 3)
    ui.event(ui.labeled('编辑边 4 长度'), 'onDoubleClick'); ui.input('草图驱动尺寸', '11')
    ui.event(ui.find(node => node.type === 'form' && node.props.className === 'fsk-dimension-form'), 'onSubmit', { preventDefault() {} })
    const edited = ui.feature(), a = edited.segments[2].to, b = edited.segments[3].to
    assert.ok(Math.abs(Math.hypot(b[0] - a[0], b[1] - a[1]) - 11) < 1e-7)
    assert.deepEqual(edited.start, b)
    assert.equal(edited.sketchConstraints.find(c => c.type === 'length').edge, 3)
    ui.click('撤销')
    assert.deepEqual(ui.feature(), rectangle)
  } finally { ui.unmount() }
})

test('real sketch dimension UI preserves box and mouse-rectangle intent through OCCT and STEP exchange',{skip:!existsSync(resolve('apps/api/.venv/bin/python'))},async()=>{
  const examples=[
    {name:'numeric-box',feature:editableProfile({id:'box1',op:'box',size:[80,50,12],origin:[3,4,5]}),parameters:{},start:[0,0]},
    {name:'parameter-box',feature:editableProfile({id:'box1',op:'box',size:['W','H','D'],origin:[3,4,5]}),parameters:{W:{value:80,source:'明确尺寸'},H:{value:50},D:{value:12}},start:[0,0]},
    {name:'mouse-rectangle',feature:{...profile(),distance:12,start:[0,0],segments:[]},parameters:{},start:[5,7],draw:true},
  ],fixtures=[]
  for(const example of examples){
    const ui=harness(example.feature,{parameters:example.parameters,variant:'canvas'})
    try{
      if(example.draw){ui.click('画矩形');ui.event(ui.svg(),'onPointerDown',ui.pointer(5,7));ui.event(ui.svg(),'onPointerDown',ui.pointer(85,57))}
      const before=structuredClone(ui.feature()),originalParameters=structuredClone(ui.parameters())
      assert.equal(before.sketchConstraints.length,5)
      ui.event(ui.labeled('编辑边 1 长度'),'onDoubleClick');ui.input('草图驱动尺寸','85')
      ui.event(ui.find(node=>node.type==='form'&&node.props.className==='fsk-dimension-form'),'onSubmit',{preventDefault(){}})
      const changed=structuredClone(ui.feature()),parameters=structuredClone(ui.parameters()),resolved=resolveSketchFeature(changed,parameters).feature,[x,y]=example.start
      assert.deepEqual([resolved.start,...resolved.segments.map(segment=>segment.to)],[[x,y],[x+85,y],[x+85,y+50],[x,y+50],[x,y]])
      if(example.name==='parameter-box'){assert.deepEqual(changed.segments,before.segments);assert.equal(parameters.W.value,85);assert.equal(parameters.W.source,'明确尺寸')}
      ui.click('撤销');assert.deepEqual(ui.feature(),before);assert.deepEqual(ui.parameters(),originalParameters)
      ui.click('重做');assert.deepEqual(ui.feature(),changed);assert.deepEqual(ui.parameters(),parameters)
      fixtures.push({name:example.name,plan:{version:'cad-plan-v1',units:'mm',parameters,features:[changed],result:changed.id}})
    }finally{ui.unmount()}
  }
  const temporary=!process.env.JOYNIU_RECTANGLE_EVIDENCE_DIR,folder=temporary?await mkdtemp(resolve('node_modules/.rectangle-geometry-')):resolve(process.env.JOYNIU_RECTANGLE_EVIDENCE_DIR)
  const script=`import sys,json,math,hashlib
from pathlib import Path
from app.cad_plan import validate_plan
from app.cad_executor import build_plan_shape
from app.geometry import get_cadquery
cq=get_cadquery();folder=Path(sys.argv[1]);folder.mkdir(parents=True,exist_ok=True);results=[]
for case in json.load(sys.stdin):
 shape,_,_=build_plan_shape(validate_plan(case['plan']))
 step=folder/(case['name']+'.step');cq.exporters.export(shape.val(),str(step),exportType='STEP')
 restored=cq.importers.importStep(str(step)).val();bounds=restored.BoundingBox()
 assert restored.isValid() and len(restored.Solids())==1
 assert all(math.isclose(actual,expected,abs_tol=1e-6) for actual,expected in zip([bounds.xlen,bounds.ylen,bounds.zlen],[85,50,12]))
 assert math.isclose(restored.Volume(),51000,rel_tol=1e-9)
 # A bounding box alone also accepts trapezoids. Check both Boolean differences
 # against an independently created exact 85x50x12 reference solid.
 reference=cq.Workplane('XY').box(85,50,12,centered=False).val().translate((bounds.xmin,bounds.ymin,bounds.zmin))
 assert restored.cut(reference).Volume()<1e-6 and reference.cut(restored).Volume()<1e-6
 (folder/(case['name']+'.plan.json')).write_text(json.dumps(case['plan'],ensure_ascii=False,indent=2))
 results.append({'name':case['name'],'valid':True,'solidCount':1,'sizeMm':[bounds.xlen,bounds.ylen,bounds.zlen],'volumeMm3':restored.Volume(),'bidirectionalDifferenceMm3':[restored.cut(reference).Volume(),reference.cut(restored).Volume()],'stepSha256':hashlib.sha256(step.read_bytes()).hexdigest()})
(folder/'verification.json').write_text(json.dumps({'method':'Actual FeatureSketchEditor event output -> validate_plan -> OCCT -> STEP readback -> bidirectional solid difference','results':results},ensure_ascii=False,indent=2))
print(json.dumps(results))`
  try{
    const result=spawnSync(resolve('apps/api/.venv/bin/python'),['-c',script,folder],{cwd:resolve('apps/api'),input:JSON.stringify(fixtures),encoding:'utf8',timeout:30000})
    assert.equal(result.status,0,result.stderr);assert.equal(JSON.parse(result.stdout).length,3)
  }finally{if(temporary)await rm(folder,{recursive:true,force:true})}
})

const expressionProfile = () => ({ ...profile(), start: ['0', '0'], segments: [{ type: 'line', to: ['width / 2', '0'] }, { type: 'line', to: ['width / 2', 'height'] }, { type: 'line', to: ['0', 'height'] }] })

test('all expression edges render; dragging and double-clicking the on-canvas dimension preserve expressions in one transaction', () => {
  const feature = expressionProfile(), ui = harness(feature, { parameters: { width: { value: 40, source: '用户确认' }, height: { value: 10 } }, variant: 'canvas' })
  try {
    assert.equal(ui.nodes().filter(node => node.type === 'path' && node.props['aria-label']?.startsWith('选择草图边')).length, 3)
    assert.doesNotMatch(ui.text(), /暂不绘制|尚未定义/)
    const point = ui.find(node => node.type === 'circle' && node.props['aria-label']?.startsWith('边 1 终点 '))
    ui.event(point, 'onPointerDown', ui.pointer(20, 0)); ui.event(ui.svg(), 'onPointerMove', ui.pointer(25, 0))
    assert.equal(ui.changes.length, 0)
    ui.event(ui.svg(), 'onPointerUp', ui.pointer(25, 0))
    assert.equal(ui.changes.length, 1); assert.equal(ui.parameters().width.value, 50)
    assert.deepEqual(ui.feature().segments, feature.segments)
    ui.event(ui.labeled('编辑边 1 长度'), 'onDoubleClick')
    ui.input('草图驱动尺寸', '30')
    ui.event(ui.find(node => node.type === 'form' && node.props.className === 'fsk-dimension-form'), 'onSubmit', { preventDefault() {} })
    assert.equal(ui.changes.length, 2); assert.equal(ui.parameters().width.value, 60)
    assert.deepEqual(ui.feature().segments, feature.segments)
    assert.equal(ui.feature().sketchConstraints[0].type, 'length')
    assert.equal(ui.parameters().width.source, '用户确认')
  } finally { ui.unmount() }
})

test('canvas line selection applies real constraints; fixed-point conflict leaves both geometry and history unchanged', () => {
  const ui = harness()
  try {
    ui.event(ui.labeled('选择草图边 1'), 'onPointerDown', ui.pointer(10, 0))
    ui.click('水平'); assert.equal(ui.feature().sketchConstraints[0].type, 'horizontal')
    ui.click('垂直'); assert.equal(ui.changes.length, 1); assert.match(ui.text(), /零长度|冲突|过约束/)
    const point = () => ui.find(node => node.type === 'circle' && node.props['aria-label']?.startsWith('边 1 终点 '))
    ui.event(point(), 'onPointerDown', ui.pointer(20, 0)); ui.event(ui.svg(), 'onPointerUp', ui.pointer(20, 0)); ui.click('固定点')
    const before = JSON.stringify(ui.feature()), history = ui.changes.length
    ui.event(point(), 'onPointerDown', ui.pointer(20, 0)); ui.event(ui.svg(), 'onPointerMove', ui.pointer(24, 3)); ui.event(ui.svg(), 'onPointerUp', ui.pointer(24, 3))
    assert.equal(JSON.stringify(ui.feature()), before); assert.equal(ui.changes.length, history)
    assert.match(ui.text(), /冲突|过约束/)
    const fixed = ui.feature().sketchConstraints.find(c => c.type === 'fixed')
    ui.event(ui.labeled(`移除草图约束 ${fixed.id}`), 'onClick')
    assert.equal(ui.feature().sketchConstraints.length, 1)
  } finally { ui.unmount() }
})

test('custom face frame stays intact and a changed parameter set cancels a stale drag', () => {
  const feature = { ...expressionProfile(), plane: 'custom', frame: { origin: [2, 3, 4], normal: [0, 1, 0], xDir: [1, 0, 0] } }; delete feature.origin
  const ui = harness(feature, { parameters: { width: { value: 40 }, height: { value: 10 } } })
  try {
    assert.equal(ui.labeled('草图平面').props.disabled, true)
    assert.equal(ui.nodes().some(node => node.props['aria-label'] === '草图原点 X'), false)
    const point = ui.find(node => node.type === 'circle' && node.props['aria-label']?.startsWith('边 1 终点 '))
    ui.event(point, 'onPointerDown', ui.pointer(20, 0)); ui.event(ui.svg(), 'onPointerMove', ui.pointer(25, 0))
    ui.update({ parameters: { width: { value: 60 }, height: { value: 10 } } })
    ui.event(ui.svg(), 'onPointerUp', ui.pointer(25, 0))
    assert.equal(ui.changes.length, 0); assert.deepEqual(ui.feature().frame, feature.frame)
  } finally { ui.unmount() }
})

test('line drag commits once; returning to the start, zoom and pan never create geometry edits', () => {
  const ui = harness()
  try {
    ui.event(ui.labeled('选择草图边 1'), 'onPointerDown', ui.pointer(10, 0))
    ui.event(ui.svg(), 'onPointerMove', ui.pointer(12, 3))
    assert.equal(ui.changes.length, 0)
    ui.event(ui.svg(), 'onPointerUp', ui.pointer(12, 3))
    assert.deepEqual(ui.feature().start, [2, 3]); assert.deepEqual(ui.feature().segments[0].to, [22, 3])
    assert.equal(ui.changes.length, 1)
    const start = ui.find(node => node.type === 'circle' && node.props['aria-label']?.startsWith('起点 '))
    ui.event(start, 'onPointerDown', ui.pointer(2, 3)); ui.event(ui.svg(), 'onPointerMove', ui.pointer(4, 5)); ui.event(ui.svg(), 'onPointerMove', ui.pointer(2, 3)); ui.event(ui.svg(), 'onPointerUp', ui.pointer(2, 3))
    assert.equal(ui.changes.length, 1, 'returning to the original position cancels the preview')
    const before = ui.svg().props.viewBox
    ui.event(ui.labeled('放大草图'), 'onClick')
    assert.notEqual(ui.svg().props.viewBox, before)
    ui.click('平移'); const initial = ui.pointer(0, 0); ui.event(ui.svg(), 'onPointerDown', initial); ui.event(ui.svg(), 'onPointerMove', { ...initial, clientX: initial.clientX + 30 }); ui.event(ui.svg(), 'onPointerUp', initial)
    assert.equal(ui.changes.length, 1)
  } finally { ui.unmount() }
})

test('canvas starts unselected and only shows a dismissible floating panel for explicit selection', () => {
  const ui = harness()
  try {
    assert.equal(ui.nodes().some(node => node.type === 'aside'), false)
    assert.equal(ui.nodes().some(node => node.type === 'circle' && node.props.className?.includes('fsk-point--selected')), false)
    assert.equal(ui.button('固定点').props.disabled, true)
    assert.equal(ui.button('删除所选').props.disabled, true)
    ui.selectPoint()
    assert.equal(ui.nodes().filter(node => node.type === 'aside').length, 1)
    ui.event(ui.labeled('关闭草图属性'), 'onClick')
    assert.equal(ui.nodes().some(node => node.type === 'aside'), false)
    ui.event(ui.labeled('选择草图边 1'), 'onPointerDown', ui.pointer(10, 0)); ui.event(ui.svg(), 'onPointerUp', ui.pointer(10, 0))
    assert.ok(ui.labeled('所选草图属性'))
    ui.event(ui.svg(), 'onPointerDown', ui.pointer(-4, -4))
    assert.equal(ui.nodes().some(node => node.type === 'aside'), false)
    assert.equal(ui.changes.length, 0)
  } finally { ui.unmount() }
})

test('the real React portal hosts the only toolbar and keeps drawing handlers active; absent host stays inline', () => {
  const host = { nodeType: 1 }, ui = harness(profile(), { toolbarHost: host })
  try {
    assert.equal(ui.portals().length, 1)
    assert.equal(ui.portals()[0].containerInfo, host)
    assert.equal(ui.inlineNodes().some(node => node.props['aria-label'] === '草图工具'), false)
    assert.equal(ui.nodes().filter(node => node.props['aria-label'] === '草图工具').length, 1)
    ui.click('画直线'); ui.event(ui.svg(), 'onPointerDown', ui.pointer(-5, 15))
    assert.deepEqual(ui.feature().segments.at(-1).to, [-5, 15])
    ui.update({ toolbarHost: null })
    assert.equal(ui.portals().length, 0)
    assert.equal(ui.inlineNodes().filter(node => node.props['aria-label'] === '草图工具').length, 1)
  } finally { ui.unmount() }
})

test('point radius and dimension text use actual screen pixels through zoom and ResizeObserver changes', () => {
  const original = globalThis.ResizeObserver; let resize, disconnected = false
  globalThis.ResizeObserver = class { constructor(callback) { resize = callback } observe() {} disconnect() { disconnected = true } }
  const ui = harness()
  const check = () => {
    const view = ui.svg().props.viewBox.split(' ').map(Number), scale = Math.min(ui.bounds.width / view[2], ui.bounds.height / view[3])
    const point = ui.find(node => node.type === 'circle' && node.props.className?.startsWith('fsk-point'))
    const text = ui.find(node => node.type === 'text')
    assert.ok(Math.abs(point.props.r * scale - 3) < 1e-9)
    assert.ok(Math.abs(text.props.fontSize * scale - 12) < 1e-9)
  }
  try {
    check(); ui.event(ui.labeled('放大草图'), 'onClick'); check()
    ui.bounds.width = 960; ui.bounds.height = 640; resize(); ui.render(); check()
    assert.equal(ui.changes.length, 0)
  } finally { ui.unmount(); assert.equal(disconnected, true); if (original === undefined) delete globalThis.ResizeObserver; else globalThis.ResizeObserver = original }
})

test('replacing an expression-constrained sketch with a circle can undo and redo the entire original sketch atomically', () => {
  const feature={...expressionProfile(),sketchConstraints:[{id:'horizontal_1',type:'horizontal',edge:0}]}
  const parameters={width:{value:40,source:{type:'user',text:'确认宽度'}},height:{value:10}},ui=harness(feature,{parameters})
  try {
    assert.equal(ui.labeled('撤销草图').props.disabled,true)
    ui.input('新图形加入方式','replace')
    ui.click('画圆');ui.event(ui.svg(),'onPointerDown',ui.pointer(5,5));ui.event(ui.svg(),'onPointerDown',ui.pointer(8,5))
    const circle=structuredClone(ui.feature());assert.equal(circle.segments.length,2);assert.deepEqual(circle.sketchConstraints.map(item=>item.type),['equal','concentric'])
    ui.click('撤销');assert.deepEqual(ui.feature(),feature);assert.deepEqual(ui.parameters(),parameters)
    assert.equal(ui.labeled('撤销草图').props.disabled,true);assert.equal(ui.labeled('重做草图').props.disabled,false)
    ui.click('重做');assert.deepEqual(ui.feature(),circle);assert.deepEqual(ui.parameters(),parameters)
    ui.click('撤销');ui.selectPoint('边 1 终点');ui.input('草图关联参数 width','44');ui.event(ui.labeled('应用草图参数 width'),'onClick')
    assert.equal(ui.parameters().width.value,44);assert.equal(ui.labeled('重做草图').props.disabled,true,'a new branch invalidates redo')
    ui.click('撤销');assert.deepEqual(ui.parameters(),parameters);assert.deepEqual(ui.feature(),feature)
  }finally{ui.unmount()}
})

test('a drag with many previews produces one undo checkpoint and cancelled or zero-distance drags produce none',()=>{
  const original=profile(),ui=harness(original)
  try{
    ui.event(ui.labeled('选择草图边 1'),'onPointerDown',ui.pointer(10,0))
    for(const point of [[11,1],[12,2],[13,3]])ui.event(ui.svg(),'onPointerMove',ui.pointer(...point))
    assert.equal(ui.labeled('撤销草图').props.disabled,true);assert.equal(ui.changes.length,0)
    ui.event(ui.svg(),'onPointerUp',ui.pointer(13,3));const moved=structuredClone(ui.feature())
    assert.equal(ui.changes.length,1);ui.click('撤销');assert.deepEqual(ui.feature(),original);assert.equal(ui.labeled('撤销草图').props.disabled,true)
    ui.click('重做');assert.deepEqual(ui.feature(),moved)
    ui.event(ui.labeled('选择草图边 1'),'onPointerDown',ui.pointer(13,3));ui.event(ui.svg(),'onPointerMove',ui.pointer(15,5));ui.event(ui.svg(),'onPointerCancel',{})
    ui.click('撤销');assert.deepEqual(ui.feature(),original);assert.equal(ui.labeled('撤销草图').props.disabled,true)
  }finally{ui.unmount()}
})

test('dimension cancellation adds no history while applied dimensions restore parameters, expressions and constraint ids',()=>{
  const feature=expressionProfile(),parameters={width:{value:40},height:{value:10}},ui=harness(feature,{parameters})
  try{
    ui.event(ui.labeled('编辑边 1 长度'),'onDoubleClick');ui.input('草图驱动尺寸','30')
    ui.event(ui.labeled('图形草图编辑器'),'onKeyDown',{key:'Escape',stopPropagation(){}})
    assert.equal(ui.changes.length,0);assert.equal(ui.labeled('撤销草图').props.disabled,true)
    ui.event(ui.labeled('编辑边 1 长度'),'onDoubleClick');ui.input('草图驱动尺寸','30')
    ui.event(ui.find(node=>node.type==='form'&&node.props.className==='fsk-dimension-form'),'onSubmit',{preventDefault(){}})
    const applied=structuredClone(ui.feature());assert.equal(ui.parameters().width.value,60);assert.equal(applied.sketchConstraints[0].type,'length')
    ui.click('撤销');assert.deepEqual(ui.feature(),feature);assert.deepEqual(ui.parameters(),parameters)
    ui.click('重做');assert.deepEqual(ui.feature(),applied);assert.equal(ui.parameters().width.value,60)
  }finally{ui.unmount()}
})

test('constraint addition, removal and deletion each undo without silently altering expressions or geometry',()=>{
  const original=profile(),ui=harness(original)
  try{
    ui.event(ui.labeled('选择草图边 1'),'onPointerDown',ui.pointer(10,0));ui.click('水平')
    const constrained=structuredClone(ui.feature()),id=constrained.sketchConstraints[0].id
    ui.event(ui.labeled(`移除草图约束 ${id}`),'onClick');assert.equal(ui.feature().sketchConstraints.length,0)
    ui.click('撤销');assert.deepEqual(ui.feature(),constrained)
    ui.click('撤销');assert.deepEqual(ui.feature(),original)
    ui.selectPoint();ui.event(ui.labeled('删除草图边 3'),'onClick');assert.equal(ui.feature().segments.length,2)
    ui.click('撤销');assert.deepEqual(ui.feature(),original)
    ui.selectPoint();ui.input('所选点 X 坐标','2');ui.input('所选点 Y 坐标','3');ui.click('应用坐标')
    ui.click('撤销');assert.deepEqual(ui.feature(),original)
  }finally{ui.unmount()}
})

test('active sketch captures platform undo shortcuts, leaves text undo alone and removes listeners when hidden or closed',()=>{
  const previous=globalThis.window,listeners=new Set();globalThis.window={addEventListener(name,callback,capture){if(name==='keydown'){assert.equal(capture,true);listeners.add(callback)}},removeEventListener(name,callback){if(name==='keydown')listeners.delete(callback)}}
  const ui=harness(),original=profile()
  const key=(options={})=>{
    const event={key:'z',ctrlKey:true,target:{tagName:'BUTTON'},defaultPrevented:false,preventDefault(){this.defaultPrevented=true},stopPropagation(){this.stopped=true},...options}
    for(const callback of [...listeners])callback(event)
    ui.render();return event
  }
  try{
    ui.click('画直线');ui.event(ui.svg(),'onPointerDown',ui.pointer(-5,15));const changed=structuredClone(ui.feature())
    assert.equal(listeners.size,1)
    const inputEvent=key({target:{tagName:'INPUT'}});assert.equal(inputEvent.defaultPrevented,false);assert.deepEqual(ui.feature(),changed)
    const undo=key({ctrlKey:false,metaKey:true});assert.equal(undo.defaultPrevented,true);assert.equal(undo.stopped,true);assert.deepEqual(ui.feature(),original)
    key({shiftKey:true});assert.deepEqual(ui.feature(),changed)
    awaitableInactive()
    function awaitableInactive(){ui.update({active:false});assert.equal(listeners.size,0);key();assert.deepEqual(ui.feature(),changed)}
    ui.update({active:true,disabled:true});assert.equal(key().defaultPrevented,false);assert.deepEqual(ui.feature(),changed)
    ui.update({disabled:false});key();assert.deepEqual(ui.feature(),original)
  }finally{ui.unmount();assert.equal(listeners.size,0);if(previous===undefined)delete globalThis.window;else globalThis.window=previous}
})

test('external parameter replacement or another sketch starts a fresh local history',()=>{
  const ui=harness(expressionProfile(),{parameters:{width:{value:40},height:{value:10}}})
  try{
    ui.selectPoint('边 1 终点');ui.input('草图关联参数 width','44');ui.event(ui.labeled('应用草图参数 width'),'onClick')
    assert.equal(ui.labeled('撤销草图').props.disabled,false)
    ui.update({parameters:{width:{value:60},height:{value:10}}});assert.equal(ui.labeled('撤销草图').props.disabled,true);ui.click('撤销');assert.equal(ui.parameters().width.value,60)
    ui.update({feature:{...profile(),id:'other_sketch'}});assert.equal(ui.labeled('撤销草图').props.disabled,true);assert.equal(ui.labeled('重做草图').props.disabled,true)
  }finally{ui.unmount()}
})

test('dimension labels have a transparent screen-sized target that blocks drawing and opens the real dimension editor',()=>{
  const ui=harness()
  const check=()=>{
    const group=ui.labeled('编辑边 1 长度'),children=React.Children.toArray(group.props.children)
    const hit=children.find(node=>node?.type==='rect'),label=children.find(node=>node?.type==='text')
    const view=ui.svg().props.viewBox.split(' ').map(Number),scale=Math.min(ui.bounds.width/view[2],ui.bounds.height/view[3])
    assert.equal(hit.props.fill,'transparent')
    assert.ok(hit.props.width*scale>=32-1e-8);assert.ok(Math.abs(hit.props.height*scale-28)<1e-8)
    assert.ok((label.props.y-hit.props.y)*scale>=18-1e-8)
    let stopped=false;ui.event(group,'onPointerDown',{stopPropagation(){stopped=true}});assert.equal(stopped,true)
    ui.event(group,'onDoubleClick');assert.equal(ui.labeled('草图驱动尺寸').props.value,'20')
  }
  try{check();ui.event(ui.labeled('放大草图'),'onClick');check();assert.equal(ui.changes.length,0)}finally{ui.unmount()}
})

test('one graphical sketch retains its rectangle, explicit hole and second outer contour with atomic radius edits and real STEP output',()=>{
  const ui=harness({...profile(),start:[0,0],segments:[]})
  try{
    ui.click('画矩形');ui.event(ui.svg(),'onPointerDown',ui.pointer(0,0));ui.event(ui.svg(),'onPointerDown',ui.pointer(40,30))
    const main=structuredClone(ui.feature().segments)
    ui.click('新增孔洞');assert.equal(ui.feature().contours[0].parent,'main')
    ui.click('画圆');ui.event(ui.svg(),'onPointerDown',ui.pointer(20,15));ui.event(ui.svg(),'onPointerDown',ui.pointer(25,15))
    assert.deepEqual(ui.feature().segments,main);assert.equal(ui.feature().contours.length,1)
    const before=structuredClone(ui.feature())
    ui.event(ui.labeled('编辑边 1 半径'),'onDoubleClick');ui.input('草图驱动尺寸','7');ui.event(ui.find(node=>node.type==='form'&&node.props.className==='fsk-dimension-form'),'onSubmit',{preventDefault(){}})
    assert.deepEqual(ui.feature().contours[0].start,[27,15]);validateSketchConstraints(ui.feature())
    const enlarged=structuredClone(ui.feature());ui.click('撤销');assert.deepEqual(ui.feature(),before);ui.click('重做');assert.deepEqual(ui.feature(),enlarged)
    ui.click('新增外轮廓');ui.click('画矩形');ui.event(ui.svg(),'onPointerDown',ui.pointer(60,0));ui.event(ui.svg(),'onPointerDown',ui.pointer(70,10))
    assert.equal(sketchContourList(ui.feature()).length,3);assert.deepEqual(ui.feature().segments,main)
    assert.equal(ui.feature().contours[0].role,'hole');assert.equal(ui.feature().contours[1].role,'outer')
    if(existsSync(resolve('apps/api/.venv/bin/python'))){
      const code=`import json,sys,tempfile,math\nfrom pathlib import Path\nfrom app.cad_plan import validate_plan\nfrom app.cad_executor import build_plan_shape\nfrom app.geometry import get_cadquery\nf=json.load(sys.stdin);cq=get_cadquery()\np={'version':'cad-plan-v1','units':'mm','features':[f],'result':f['id']}\ns,_,_=build_plan_shape(validate_plan(p))\nwith tempfile.TemporaryDirectory() as d:\n path=Path(d)/'multi.step';cq.exporters.export(s.val(),str(path),exportType='STEP');r=cq.importers.importStep(str(path)).val()\n assert r.isValid() and len(r.Solids())==2\n assert math.isclose(r.Volume(),(40*30+10*10-math.pi*49)*5,rel_tol=1e-7),r.Volume()\n print(r.Volume())`
      const result=spawnSync(resolve('apps/api/.venv/bin/python'),['-c',code],{cwd:resolve('apps/api'),input:JSON.stringify(ui.feature()),encoding:'utf8',timeout:30000});assert.equal(result.status,0,result.stderr)
    }
  }finally{ui.unmount()}
})

test('ellipse and spline tools use only user points and retain exact curve definitions through undo and real kernel building',()=>{
  const cases=[]
  for(const tool of ['ellipse','spline']){
    const ui=harness({...profile(),start:[0,0],segments:[]})
    try{
      if(tool==='ellipse'){
        ui.click('画椭圆');ui.event(ui.svg(),'onPointerDown',ui.pointer(0,0));ui.event(ui.svg(),'onPointerDown',ui.pointer(10,0));assert.equal(ui.changes.length,0)
        ui.event(ui.svg(),'onPointerDown',ui.pointer(0,5));assert.equal(ui.feature().segments.length,1);assert.equal(ui.feature().segments[0].type,'ellipse')
        ui.event(ui.labeled('编辑边 1 长轴半径'),'onDoubleClick');ui.input('草图驱动尺寸','12');ui.event(ui.find(node=>node.type==='form'&&node.props.className==='fsk-dimension-form'),'onSubmit',{preventDefault(){}})
        assert.deepEqual(ui.feature().segments[0].radii,[12,5]);cases.push({feature:ui.feature(),volume:300*Math.PI})
      }else{
        ui.click('画样条');for(const point of [[0,0],[5,8],[10,0]])ui.event(ui.svg(),'onPointerDown',ui.pointer(...point));assert.equal(ui.changes.length,0)
        ui.click('完成样条');assert.deepEqual(ui.feature().segments[0],{type:'spline',through:[[5,8]],to:[10,0]})
        assert.ok(ui.nodes().some(node=>node.type==='path'&&node.props.d?.includes(' C ')))
        ui.click('画直线');ui.event(ui.svg(),'onPointerDown',ui.pointer(10,-5));ui.event(ui.svg(),'onPointerDown',ui.pointer(0,-5));cases.push({feature:ui.feature(),volume:1450/3})
      }
      const final=structuredClone(ui.feature());ui.click('撤销');ui.click('重做');assert.deepEqual(ui.feature(),final)
    }finally{ui.unmount()}
  }
  if(existsSync(resolve('apps/api/.venv/bin/python'))){
    const code=`import json,sys,tempfile,math\nfrom pathlib import Path\nfrom app.cad_plan import validate_plan\nfrom app.cad_executor import build_plan_shape\nfrom app.geometry import get_cadquery\ncq=get_cadquery()\nfor case in json.load(sys.stdin):\n f=case['feature'];s,_,_=build_plan_shape(validate_plan({'version':'cad-plan-v1','units':'mm','features':[f],'result':f['id']}))\n with tempfile.TemporaryDirectory() as d:\n  p=Path(d)/'curve.step';cq.exporters.export(s.val(),str(p),exportType='STEP');r=cq.importers.importStep(str(p)).val()\n  assert r.isValid() and len(r.Solids())==1\n  assert math.isclose(r.Volume(),case['volume'],rel_tol=1e-7),(r.Volume(),case['volume'])`
    const result=spawnSync(resolve('apps/api/.venv/bin/python'),['-c',code],{cwd:resolve('apps/api'),input:JSON.stringify(cases),encoding:'utf8',timeout:30000});assert.equal(result.status,0,result.stderr)
  }
})

test('drawing another closed shape adds a distinct contour by default and deleting it can be undone',()=>{
  const initial=profile(),ui=harness(initial)
  try{
    ui.click('画圆');ui.event(ui.svg(),'onPointerDown',ui.pointer(35,5));ui.event(ui.svg(),'onPointerDown',ui.pointer(38,5))
    assert.deepEqual(ui.feature().segments,initial.segments);assert.equal(ui.feature().contours[0].role,'outer')
    const added=structuredClone(ui.feature());ui.click('删除轮廓');assert.equal(ui.feature().contours.length,0)
    ui.click('撤销');assert.deepEqual(ui.feature(),added)
  }finally{ui.unmount()}
})

test('graphical cross-contour edge selection applies actual relationships and persisted dimension values undo atomically',()=>{
  const f={...profile(),segments:[{type:'line',to:[20,1]},{type:'line',to:[20,10]},{type:'line',to:[0,10]},{type:'line',to:[0,0]}],contours:[{id:'other',role:'outer',start:[40,0],segments:[{type:'line',to:[50,3]},{type:'line',to:[50,10]},{type:'line',to:[40,10]},{type:'line',to:[40,0]}]}]},ui=harness(f)
  try{
    ui.event(ui.labeled('选择草图边 1'),'onPointerDown',ui.pointer(10,.5));ui.event(ui.labeled('选择轮廓 other 边 1'),'onPointerDown',{...ui.pointer(45,1.5),shiftKey:true})
    assert.equal(ui.button('应用关系 (2)').props.disabled,false);ui.input('几何约束类型','parallel');ui.click('应用关系 (2)')
    const c=ui.feature().sketchConstraints[0];assert.equal(c.type,'parallel');assert.deepEqual(c.edges,[0,{contourId:'other',edge:0}]);assert.equal(validateSketchConstraints(ui.feature()),true)
    const constrained=structuredClone(ui.feature());ui.click('撤销');assert.deepEqual(ui.feature(),f);ui.click('重做');assert.deepEqual(ui.feature(),constrained)
    ui.event(ui.labeled('选择草图边 1'),'onPointerDown',ui.pointer(45,1));ui.event(ui.labeled('选择草图边 2'),'onPointerDown',{...ui.pointer(50,6),ctrlKey:true})
    ui.input('几何约束类型','angle');ui.input('约束驱动值','60');ui.click('应用关系 (2)');assert.equal(validateSketchConstraints(ui.feature()),true)
    const angle=ui.feature().sketchConstraints.find(c=>c.type==='angle');assert.ok(angle)
    ui.input(`约束 ${angle.id} 驱动值`,'45');ui.event(ui.labeled(`更新约束 ${angle.id}`),'onClick');assert.equal(ui.feature().sketchConstraints.find(c=>c.id===angle.id).value,45);assert.equal(validateSketchConstraints(ui.feature()),true)
  }finally{ui.unmount()}
})

test('cross-contour point coincidence uses explicit point references and keeps the edit undoable',()=>{
  const f={...profile(),contours:[{id:'other',role:'outer',start:[2,2],segments:[{type:'line',to:[30,2]},{type:'line',to:[30,20]},{type:'line',to:[2,20]},{type:'line',to:[2,2]}]}]},ui=harness(f)
  try{
    ui.selectPoint();ui.event(ui.labeled('选择轮廓 other 起点'),'onPointerDown',{...ui.pointer(2,2),shiftKey:true})
    assert.equal(ui.button('重合').props.disabled,false);ui.click('重合')
    const c=ui.feature().sketchConstraints[0];assert.equal(c.type,'coincident');assert.deepEqual(c.points,['start',{contourId:'other',point:'start'}]);assert.equal(validateSketchConstraints(ui.feature()),true)
    const a=ui.feature().start,b=ui.feature().contours[0].start;assert.ok(Math.hypot(a[0]-b[0],a[1]-b[1])<1e-5)
    ui.click('撤销');assert.deepEqual(ui.feature(),f)
  }finally{ui.unmount()}
})

test('inactive contour geometry does not steal drawing gestures and unrelated constraints do not block edge editing',()=>{
  const f={...profile(),sketchConstraints:[{id:'main_h',type:'horizontal',edge:0}],contours:[{id:'other',role:'outer',start:[40,0],segments:[{type:'spline',through:[[43,5],[46,4],[48,3]],to:[50,0]},{type:'line',to:[40,-5]},{type:'line',to:[40,0]}]}]},ui=harness(f)
  try{
    ui.input('当前草图轮廓','other');ui.selectPoint('边 1 样条插值点 2');ui.click('删除所选');assert.deepEqual(ui.feature().contours[0].segments[0].through,[[43,5],[48,3]]);assert.deepEqual(ui.feature().sketchConstraints,f.sketchConstraints)
    ui.click('新增外轮廓');ui.click('画矩形');ui.event(ui.labeled('选择轮廓 main 边 1'),'onPointerDown',ui.pointer(3,0));assert.equal(ui.labeled('当前草图轮廓').props.value,'contour1')
    ui.event(ui.svg(),'onPointerDown',ui.pointer(7,4));assert.deepEqual(ui.feature().contours.find(item=>item.id==='contour1').start,[3,0]);assert.equal(ui.feature().contours.find(item=>item.id==='contour1').segments.length,4)
  }finally{ui.unmount()}
})

test('inactive sketches cancel transient drawing and drag intent without mutating their retained draft',()=>{
  const f=profile(),ui=harness(f)
  try{
    ui.event(ui.find(node=>node.type==='circle'&&node.props['aria-label']?.startsWith('起点 ')),'onPointerDown',ui.pointer(0,0));ui.event(ui.svg(),'onPointerMove',ui.pointer(1,1))
    assert.equal(ui.changes.length,0);ui.update({active:false});ui.event(ui.svg(),'onPointerUp',ui.pointer(1,1));assert.deepEqual(ui.feature(),f);assert.equal(ui.changes.length,0)
    ui.update({active:true});ui.click('画样条');ui.event(ui.svg(),'onPointerDown',ui.pointer(5,15));ui.update({active:false});ui.update({active:true})
    assert.equal(ui.nodes().filter(node=>node.type==='circle'&&node.props.className==='fsk-pending').length,0);assert.equal(ui.changes.length,0)
  }finally{ui.unmount()}
})

test('profile sweep edits exactly its own section and leaves path and orientation settings untouched',()=>{
  const f={...profile(),op:'profile_sweep',path:{start:[0,0,0],segments:[{type:'line',to:[0,0,30]}]},transition:'transformed'};delete f.distance
  const ui=harness(f)
  try{
    ui.event(ui.labeled('编辑边 1 长度'),'onDoubleClick');ui.input('草图驱动尺寸','24');ui.event(ui.find(node=>node.type==='form'&&node.props.className==='fsk-dimension-form'),'onSubmit',{preventDefault(){}})
    assert.deepEqual(ui.feature().path,f.path);assert.equal(ui.feature().transition,'transformed');assert.equal(ui.feature().op,'profile_sweep');assert.notDeepEqual(ui.feature().segments,f.segments)
  }finally{ui.unmount()}
})
