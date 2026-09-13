import {useState} from 'react'
import {ScalarField,VectorField} from './CadScalarFields.jsx'
import {baseReferenceFrames,editorEntityId} from './cadEditorEntities.js'
import {cloneFeatureValue} from './directFeatureModel.js'
import {evaluateSketchExpression} from './featureSketchExpressions.js'
import {detachSweepPath,sweepPathFromSketch,sweepSketchFrame} from './cadSweepPathModel.js'
import CadSweepPathEditor from './CadSweepPathEditor.jsx'
import './cad-profile-tools.css'

const blankPoint=()=>['','','']
function PathSegmentFields({segment,index,onChange,onRemove}){
  const label=`路径 ${index+1}`
  const patch=(key,value)=>onChange({...segment,[key]:value})
  const changeType=type=>{
    const next={type,to:cloneFeatureValue(segment.to)}
    if(type==='arc')next.through=blankPoint()
    if(type==='spline')next.through=[blankPoint()]
    onChange(next)
  }
  return <div className="df-segment">
    <div className="df-row"><b>{label}</b><select aria-label={`路径段 ${index+1} 类型`} value={segment.type} onChange={e=>changeType(e.target.value)}>
      <option value="line">直线</option><option value="arc">三点圆弧</option><option value="spline">样条曲线</option>{segment.type==='ellipse'&&<option value="ellipse">椭圆曲线</option>}
    </select><button type="button" aria-label={`删除路径段 ${index+1}`} onClick={onRemove}>×</button></div>
    {segment.type==='arc'&&<VectorField label={`${label} 圆弧经过点`} value={segment.through} onChange={v=>patch('through',v)}/>}
    {segment.type==='spline'&&<>
      {(segment.through||[]).map((point,k)=><div key={k}><VectorField label={`${label} 样条点 ${k+1}`} value={point} onChange={v=>patch('through',segment.through.map((p,j)=>j===k?v:p))}/><button type="button" aria-label={`删除路径 ${index+1} 样条点 ${k+1}`} disabled={segment.through.length<=1} onClick={()=>patch('through',segment.through.filter((_,j)=>j!==k))}>删除插值点</button></div>)}
      <button type="button" disabled={segment.through.length>=62} onClick={()=>patch('through',[...segment.through,blankPoint()])}>添加样条点</button>
    </>}
    {segment.type==='ellipse'&&<>
      <p className="ce-panel-note">优先编辑所引用的椭圆草图；直接改这些字段会解除关联，起止点必须与椭圆参数一致。</p>
      {['center','xDir','normal'].map(key=><VectorField key={key} label={`${label} ${{center:'椭圆中心',xDir:'局部 X 方向',normal:'平面法线'}[key]}`} value={segment[key]} onChange={v=>patch(key,v)}/>)}
      <VectorField label={`${label} 椭圆半轴`} dimensions={2} axes={['a','b']} value={segment.radii} onChange={v=>patch('radii',v)}/>
      {['rotation','startAngle','endAngle'].map(key=><ScalarField key={key} label={`${label} ${{rotation:'方向角',startAngle:'起始角',endAngle:'终止角'}[key]}`} value={segment[key]} onChange={v=>patch(key,v)}/>)}
    </>}
    <VectorField label={`${label} 终点`} value={segment.to} onChange={v=>patch('to',v)}/>
  </div>
}

export function CadSweepFields({feature,onChange,sketches=[],parameters={},references=[],onPickPath,disabled=false}){
  const [error,setError]=useState(''),path=feature.path||{start:blankPoint(),segments:[]}
  const patch=value=>{if(disabled)return;setError('');onChange({...feature,path:value})}
  const manual=value=>patch(detachSweepPath(value))
  const selectSketch=id=>{
    try{
      if(!id){manual(path.curveReference?{start:[0,0,0],segments:[]}:path);return}
      const sketch=sketches.find(s=>s.id===id)
      if(!sketch)throw new Error('所选路径草图已不存在。')
      patch(sweepPathFromSketch(sketch,parameters,references))
    }catch(reason){setError(reason.message)}
  }
  return <section className="df-contour">
    <h4>扫掠路径</h4>
    <label className="df-field"><span>使用草图主轮廓作为路径</span><select aria-label="扫掠路径草图" value={path.sketchId||''} onChange={e=>selectSketch(e.target.value)}>
      <option value="">直接定义空间路径</option>{path.sketchId&&!sketches.some(s=>s.id===path.sketchId)&&<option value={path.sketchId} disabled>{path.sketchId}（来源不可用）</option>}
      {sketches.filter(s=>s.id!==feature.sketchId).map(s=><option value={s.id} key={s.id}>{s.label||s.id}</option>)}
    </select></label>
    {references.some(r=>r.kind==='helix')&&<label className="df-field"><span>使用参考曲线</span><select aria-label="扫掠参考曲线" value={path.curveReference||''} onChange={e=>patch(e.target.value?{curveReference:e.target.value}:{start:[0,0,0],segments:[]})}><option value="">不使用参考曲线</option>{references.filter(r=>r.kind==='helix').map(r=><option key={r.id} value={r.id}>{r.label||r.id}</option>)}</select></label>}
    {path.curveReference&&<p className="ce-panel-note">关联螺旋线；调整参考曲线的螺距、半径和圈数后会重新扫掠。</p>}
    {path.sketchId&&<p className="ce-panel-note">关联草图的主轮廓；附加轮廓不会串入路径。重建时读取该草图的最新参数。</p>}
    {onPickPath&&!path.curveReference&&<button type="button" onClick={onPickPath}>在模型上点选路径</button>}
    {!path.curveReference&&<><CadSweepPathEditor key={feature.id} path={path} parameters={parameters} onChange={patch} disabled={disabled}/>
    <details className="cad-path-advanced"><summary>路径坐标、表达式与曲线定义</summary>
      <VectorField label="路径起点" value={path.start} onChange={start=>manual({...path,start})}/>
      {(path.segments||[]).map((segment,index)=><PathSegmentFields key={index} segment={segment} index={index} onChange={value=>manual({...path,segments:path.segments.map((s,i)=>i===index?value:s)})} onRemove={()=>manual({...path,segments:path.segments.filter((_,i)=>i!==index)})}/>)}
      <button type="button" disabled={path.segments.length>=128} onClick={()=>manual({...path,segments:[...(path.segments||[]),{type:'line',to:blankPoint()}]})}>＋ 路径段</button>
    </details></>}
    <label className="df-field"><span>转角方式</span><select aria-label="扫掠转角方式" value={feature.transition||'transformed'} onChange={e=>onChange({...feature,transition:e.target.value})}><option value="transformed">平滑跟随</option><option value="right">斜接</option><option value="round">圆角转接</option></select></label>
    <label className="df-check"><input type="checkbox" checked={Boolean(feature.isFrenet)} onChange={e=>onChange({...feature,isFrenet:e.target.checked})}/>沿路径法标架旋转截面</label>
    <p className="ce-panel-note">截面须位于路径起点，并与起始方向垂直；预览与重建会检查路径、截面和自交。</p>
    {error&&<p className="df-error" role="alert">{error}</p>}
  </section>
}

export function CadLoftFields({feature,onChange,sketches=[],parameters={},references=[],onSketch}){
  const [selected,setSelected]=useState(''),[error,setError]=useState(''),sections=feature.sections||[]
  const publish=next=>{setError('');onChange(next)}
  const patch=(index,value)=>publish({...feature,sections:sections.map((s,i)=>i===index?value:s)})
  const addExisting=()=>{
    try{
      const sketch=sketches.find(s=>s.id===selected)
      if(!sketch?.segments?.length)throw new Error('请先绘制所选截面的闭合轮廓。')
      const section={id:editorEntityId('section',sections),sketchId:sketch.id,frame:sweepSketchFrame(sketch,parameters,references),...Object.fromEntries(['start','segments','contours','sketchConstraints'].filter(key=>Object.hasOwn(sketch,key)).map(key=>[key,cloneFeatureValue(sketch[key])]))}
      publish({...feature,sections:[...sections,section]});setSelected('')
    }catch(reason){setError(reason.message)}
  }
  const addNew=()=>{
    const section={id:editorEntityId('section',sections),frame:cloneFeatureValue(baseReferenceFrames.XY),start:[0,0],segments:[]},next={...feature,sections:[...sections,section]}
    publish(next);onSketch?.(sections.length,next)
  }
  return <section className="df-contour">
    <h4>放样截面</h4>
    {!sections.length&&<p className="cad-loft-empty">先添加两个以上截面，按列表顺序连接。</p>}
    {sections.map((section,index)=><div className="df-segment" key={section.id}>
      <div className="df-row"><b>{index+1}. {sketches.find(s=>s.id===section.sketchId)?.label||section.id}</b>
        <button type="button" aria-label={`上移截面 ${index+1}`} disabled={!index} onClick={()=>{const next=[...sections];[next[index-1],next[index]]=[next[index],next[index-1]];publish({...feature,sections:next})}}>↑</button>
        <button type="button" aria-label={`删除截面 ${index+1}`} onClick={()=>publish({...feature,sections:sections.filter((_,i)=>i!==index)})}>×</button>
      </div>
      <button type="button" disabled={!onSketch} onClick={()=>onSketch?.(index,feature)}>编辑截面草图</button>
      {section.sketchId?<p className="ce-panel-note">关联独立草图 {section.sketchId}，通过草图编辑其形状与平面。</p>:Object.entries({origin:'原点',xDir:'X 方向',normal:'法线'}).map(([key,label])=><VectorField key={key} label={`截面 ${index+1} ${label}`} value={section.frame?.[key]} onChange={value=>patch(index,{...section,frame:{...section.frame,[key]:value}})}/>)}
    </div>)}
    <label className="df-field"><span>添加独立草图截面</span><select aria-label="放样截面草图" value={selected} onChange={e=>setSelected(e.target.value)}><option value="">选择草图</option>{sketches.map(s=><option value={s.id} key={s.id}>{s.label||s.id}</option>)}</select></label>
    <div className="cad-profile-actions"><button type="button" disabled={!selected||sections.length>=12} onClick={addExisting}>添加所选截面</button><button type="button" disabled={sections.length>=12||!onSketch} onClick={addNew}>＋ 绘制新截面</button></div>
    <p className="ce-panel-note">按顺序连接至少两个截面；各截面保留平面、轮廓和孔洞。编辑新截面时请指定其平面位置。</p>
    {error&&<p role="alert" className="df-error">{error}</p>}
  </section>
}

export function CadSurfaceGridFields({feature,onChange,onEditGrid,editingGrid=false,parameters={}}){
  const [size,setSize]=useState([20,20]),[counts,setCounts]=useState([3,3]),[error,setError]=useState(''),points=feature.points||[]
  const createGrid=()=>{
    try{
      const [rows,cols]=counts.map(v=>evaluateSketchExpression(v,parameters)),[width,height]=size.map(v=>evaluateSketchExpression(v,parameters))
      if(![rows,cols].every(n=>Number.isInteger(n)&&n>=2&&n<=16))throw new Error('网格行列必须是2至16之间的整数。')
      if(![width,height].every(n=>n>0))throw new Error('网格宽度和高度必须大于零。')
      onChange({...feature,points:Array.from({length:rows},(_,i)=>Array.from({length:cols},(_,j)=>[width*j/(cols-1),height*i/(rows-1),0]))});setError('')
    }catch(reason){setError(reason.message)}
  }
  return <section className="df-contour">
    <h4>曲面控制网格</h4>
    {!points.length&&<><VectorField label="网格宽高" dimensions={2} value={size} onChange={v=>{setSize(v);setError('')}}/><VectorField label="网格行列" dimensions={2} value={counts} onChange={v=>{setCounts(v);setError('')}}/><button type="button" onClick={createGrid}>创建控制网格</button></>}
    {points.length>0&&onEditGrid&&<button type="button" aria-pressed={editingGrid} onClick={onEditGrid}>{editingGrid?'结束控制点拖动':'在模型视图拖动控制点'}</button>}
    {editingGrid&&<p className="ce-panel-note" role="status">在模型视图拖动网格控制点；下方精确坐标同步更新，完成操作后才保存。</p>}
    <div className="ce-surface-grid">{points.flatMap((row,i)=>row.map((point,j)=><VectorField key={`${i}-${j}`} label={`控制点 ${i+1},${j+1}`} value={point} onChange={value=>onChange({...feature,points:points.map((r,k)=>k===i?r.map((p,l)=>l===j?value:p):r)})}/>))}</div>
    <ScalarField label="拟合公差 (mm)" value={feature.tolerance??0.01} onChange={tolerance=>onChange({...feature,tolerance})}/>
    {error&&<p className="df-error cad-grid-error" role="alert">{error}</p>}
  </section>
}
