import { useRef } from 'react'
import './studio-home.css'

const examples = [
  ['安装支架', '设计一个 L 形安装支架：两边长 60 mm 和 40 mm，宽 30 mm，厚 5 mm。每边中心开一个直径 6 mm 的通孔。'],
  ['法兰盘', '设计一个外径 80 mm、厚 10 mm 的法兰盘，中心通孔直径 20 mm，直径 60 mm 的分度圆上均布 4 个直径 6 mm 的通孔。'],
  ['基础长方体', '创建一个长 40 mm、宽 20 mm、高 10 mm 的实心长方体，不需要孔、倒角或圆角。'],
]

export default function StudioHome({ projects, onSelectProject, onStartText, setActiveMode, attachDrawingToConversation, description, setDescription }) {
  const inputRef = useRef(null)
  const promptRef = useRef(null)
  return <div className="studio-home">
    <section className="studio-home-start" aria-label="开始 AI 设计">
      <div className="studio-home-intro"><span>创模 AI</span><h1>你想设计什么？</h1><p>描述零件，或上传一张工程图。</p></div>
      <div className="studio-composer">
        <textarea ref={promptRef} aria-label="描述零件设计" value={description} onChange={event => setDescription(event.target.value)} placeholder="例如：设计一个带 4 个安装孔的法兰盘…" />
        <div className="studio-composer-actions">
          <button type="button" className="studio-attach" onClick={() => inputRef.current?.click()}><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m8 13 7-7a3 3 0 0 1 4 4l-9 9a5 5 0 0 1-7-7l9-9" /></svg>添加图纸</button>
          <span>图片、PDF、DWG、DXF</span>
          <button type="button" className="studio-start-button" aria-label="开始文字设计" disabled={!description.trim()} onClick={() => onStartText(description)}>开始设计 <span aria-hidden="true">↑</span></button>
        </div>
        <input ref={inputRef} hidden type="file" accept="image/*,.pdf,.dxf,.dwg" aria-label="上传图纸开始 AI 分析" onChange={event => { const files = Array.from(event.currentTarget.files || []); event.currentTarget.value = ''; if (files.length) attachDrawingToConversation?.(files, description) }} />
      </div>
      <div className="studio-prompt-examples"><span>试着描述</span>{examples.map(([label, prompt]) => <button type="button" key={label} onClick={() => { setDescription(prompt); promptRef.current?.focus() }}>{label}</button>)}</div>
    </section>
    <section className="studio-home-recent" aria-label="最近项目">
      <div className="studio-section-heading"><h2>最近项目</h2><button type="button" onClick={() => setActiveMode('项目管理')}>查看全部 <span aria-hidden="true">→</span></button></div>
      {projects.length ? <div className="studio-home-recent-list">{[...projects].sort((a, b) => String(b.updatedAt || '').localeCompare(String(a.updatedAt || ''))).slice(0, 5).map(project => <button type="button" className="studio-project-row" key={project.id} onClick={() => onSelectProject(project.id)}><span className="studio-project-symbol" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="m12 3 8 4.5v9L12 21l-8-4.5v-9L12 3ZM4 7.5l8 5 8-5M12 12.5V21" /></svg></span><span className="studio-home-project-name"><b>{project.name}</b><small>{project.files} 个文件</small></span><span className="studio-project-updated">{project.updated}</span><span className="studio-project-open" aria-hidden="true">→</span></button>)}</div> : <p className="studio-no-projects">开始第一份设计后，可以在这里继续打开。</p>}
    </section>
  </div>
}
