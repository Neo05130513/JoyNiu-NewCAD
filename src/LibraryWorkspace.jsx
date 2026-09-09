import { useMemo, useState } from 'react'
import { createPartInstance, initialPartPosition, mainModelEnvelope, standardParts, validatePartDefinition } from './assemblyModel.js'
import './assembly-workspace.css'

const initialCustom = { name: '', shape: 'cylinder', outerDiameter: '20', innerDiameter: '10', length: '30' }

export default function LibraryWorkspace({ model, assemblyItems = [], setAssemblyItems, onOpenAssembly, onBackToModel, showToast }) {
  const [query, setQuery] = useState('')
  const [group, setGroup] = useState('全部')
  const [customOpen, setCustomOpen] = useState(false)
  const [custom, setCustom] = useState(initialCustom)
  const [customErrors, setCustomErrors] = useState([])
  const [notice, setNotice] = useState('')
  const envelope = mainModelEnvelope(model)
  const instanceCount = assemblyItems.length + (envelope ? 1 : 0)
  const filtered = useMemo(() => standardParts.filter((part) => (group === '全部' || part.group === group) && `${part.name} ${part.spec} ${part.source}`.toLowerCase().includes(query.trim().toLowerCase())), [query, group])
  const insert = (part) => {
    const instance = createPartInstance(part, { position: initialPartPosition(model, assemblyItems, part) })
    setAssemblyItems((items) => [...items, instance])
    const text = `已插入 ${instance.name} ${instance.spec}，可在装配页调整位置。`
    setNotice(text)
    showToast?.(text, 'success')
  }
  const insertCustom = (event) => {
    event.preventDefault()
    const part = { name: custom.name, shape: custom.shape, group: '自定义', dimensions: { outerDiameter: custom.outerDiameter, length: custom.length, innerDiameter: custom.shape === 'ring' ? custom.innerDiameter : 0 }, source: '用户自定义尺寸 · 公称包络', spec: `${custom.shape === 'ring' ? `内径 Ø${custom.innerDiameter} / ` : ''}外径 Ø${custom.outerDiameter} × ${custom.length}` }
    const errors = validatePartDefinition(part)
    setCustomErrors(errors)
    if (errors.length) return
    insert(part)
    setCustomOpen(false)
    setCustom(initialCustom)
  }
  return <div className="aw-workspace">
    <header className="aw-heading"><div><span className="aw-eyebrow">STANDARD PARTS</span><h1>标准件库</h1><p>将有尺寸的独立实例加入「{model?.name || '当前零件'}」的装配。</p></div><div className="aw-actions"><button onClick={() => setCustomOpen((open) => !open)}>＋ 自定义零件</button><button className="aw-primary" onClick={onOpenAssembly}>查看装配（{instanceCount}） →</button></div></header>
    <div className="aw-library-layout"><main className="aw-card aw-library-main">
      <div className="aw-library-tools"><label className="aw-search"><span>⌕</span><input aria-label="搜索标准件" placeholder="搜索名称或规格，如 M8、6204" value={query} onChange={(event) => setQuery(event.target.value)} /></label><div className="aw-filters" aria-label="标准件分类">{['全部', '紧固件', '轴承', '定位件'].map((item) => <button key={item} className={group === item ? 'selected' : ''} aria-pressed={group === item} onClick={() => setGroup(item)}>{item}</button>)}</div></div>
      {customOpen && <form className="aw-custom-form" onSubmit={insertCustom}><h2>自定义圆柱 / 环形件</h2><p>按毫米保存尺寸，几何沿局部 X 轴。复杂轮廓请使用建模工作台。</p><div className="aw-custom-fields"><label>名称<input autoFocus required maxLength={80} value={custom.name} onChange={(event) => setCustom({ ...custom, name: event.target.value })} placeholder="例如：隔套" /></label><label>形状<select value={custom.shape} onChange={(event) => setCustom({ ...custom, shape: event.target.value })}><option value="cylinder">实心圆柱</option><option value="ring">环形套筒</option></select></label>{[['outerDiameter', '外径'], ['length', '长度'], ...(custom.shape === 'ring' ? [['innerDiameter', '内径']] : [])].map(([key, label]) => <label key={key}>{label} / mm<input type="number" min="0.001" max="100000" step="any" required value={custom[key]} onChange={(event) => setCustom({ ...custom, [key]: event.target.value })} /></label>)}</div>{customErrors.length > 0 && <div className="aw-error" role="alert">{customErrors.join(' ')}</div>}<div className="aw-actions"><button type="button" onClick={() => { setCustomOpen(false); setCustomErrors([]) }}>取消</button><button className="aw-primary" type="submit">创建并插入</button></div></form>}
      <div className="aw-library-list">{filtered.map((part) => <article className="aw-library-item" key={part.catalogId}><div className={`aw-part-symbol ${part.shape}`} aria-hidden="true">{part.shape === 'ring' ? '◎' : part.shape === 'bolt' ? '⬡' : '▰'}</div><div className="aw-part-copy"><h2>{part.name}</h2><p>{part.spec}</p><small>{part.source}</small></div><button aria-label={`插入 ${part.name} ${part.spec}`} onClick={() => insert(part)}>＋ 插入</button></article>)}</div>
      {!filtered.length && <div className="aw-empty"><h2>没有匹配的规格</h2><p>可调整搜索，或创建有明确尺寸的自定义件。</p><button onClick={() => { setQuery(''); setGroup('全部') }}>清除筛选</button></div>}
      <p className="aw-scope">内置 {standardParts.length} 项公称尺寸。预览保留外形与通孔；螺纹牙型、轴承滚道和制造公差未建模。</p>
    </main><aside className="aw-card aw-library-side"><h2>当前装配</h2>{envelope ? <div className="aw-current-model"><span>◇</span><div><b>{model?.name || '当前零件'}</b><small>当前模型 · 固定基准</small></div></div> : <p className="aw-muted">尚未创建主件，可先插入标准件并独立调整位置。</p>}<div className="aw-inserted-list">{assemblyItems.map((item, index) => <div key={item.id}><span>{String(index + 1).padStart(2, '0')}</span><div><b>{item.name}</b><small>{item.spec}</small></div></div>)}</div>{!assemblyItems.length && <p className="aw-muted">还没有插入标准件。每次插入都会创建一个可独立移动的实例。</p>}<button className="aw-primary aw-wide" onClick={onOpenAssembly}>进入装配调整位置</button>{onBackToModel && <button className="aw-wide" onClick={onBackToModel}>返回 3D 建模</button>}<p className="aw-scope">规格、来源和位置随当前文件保存，切换文件可建立独立装配。</p></aside></div>
    <div className="aw-notice" role="status" aria-live="polite">{notice}</div>
  </div>
}
