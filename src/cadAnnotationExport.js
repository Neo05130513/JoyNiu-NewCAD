const xml = value => String(value ?? '').replace(/[<>&"']/g, c => ({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;',"'":'&apos;'}[c]))
export function cadAnnotatedSvg({image,width,height,annotations=[],title='零件 PMI'}) {
  if(!/^data:image\/png;base64,[A-Za-z0-9+/=]+$/.test(image)||!Number.isFinite(width)||width<=0||!Number.isFinite(height)||height<=0)throw new Error('画布尚未就绪，请等待模型加载。')
  const marks=annotations.filter(a=>!a.hidden&&a.position?.every(Number.isFinite)).map(note=>{
    const [x,y]=note.position,lines=String(note.displayText||'').split('\n'),points=(note.points||[]).filter(p=>p.every(Number.isFinite)).map(p=>p.join(',')).join(' ')
    return `<g><polyline points="${points} ${x},${y}" fill="none" stroke="#513764" stroke-width="1.4"/><rect x="${x-5}" y="${y-16}" width="${Math.max(...lines.map(l=>l.length),1)*13+10}" height="${lines.length*19+5}" fill="white" fill-opacity=".95" stroke="#a095ac"/><text x="${x}" y="${y}" fill="#382548" font-family="system-ui,sans-serif" font-size="13">${lines.map((line,i)=>`<tspan x="${x}" dy="${i?19:0}">${xml(line)}</tspan>`).join('')}</text></g>`
  }).join('')
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height+44}" viewBox="0 0 ${width} ${height+44}"><title>${xml(title)}</title><rect width="100%" height="100%" fill="white"/><image href="${image}" width="${width}" height="${height}"/>${marks}<text x="16" y="${height+28}" font-family="system-ui,sans-serif" font-size="14" fill="#26364b">${xml(title)} · 单位 mm</text></svg>`
}
