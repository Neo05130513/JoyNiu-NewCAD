import { featureScalar } from './directFeatureModel.js'
const scalarText = value => typeof value === 'string' || typeof value === 'number' ? value : ''
export function ScalarField({ label, value, onChange }) {
  const unsupported = value !== null && value !== undefined && !['number','string'].includes(typeof value)
  return <label className="df-field"><span>{label}</span><input aria-label={label} value={scalarText(value)} maxLength={256} placeholder="数值或表达式" aria-invalid={unsupported || undefined} onChange={event => onChange(featureScalar(event.target.value))} />{unsupported && <small role="alert">原值格式异常，请明确填写尺寸。</small>}</label>
}
export function VectorField({ label, value, onChange, dimensions = 3, axes }) {
  const vector = Array.from({length:dimensions}, (_item,index) => Array.isArray(value) ? value[index] : undefined)
  const invalid = !Array.isArray(value) || value.length !== dimensions || value.some(item => item !== null && !['number','string'].includes(typeof item))
  const names = axes || (dimensions === 2 ? ['U','V'] : ['X','Y','Z'])
  return <div className="df-field"><span>{label}</span><div className="df-vector">{vector.map((item,index) => <input key={index} aria-label={`${label} ${names[index]}`} value={scalarText(item)} maxLength={256} placeholder={names[index]} aria-invalid={invalid || undefined} onChange={event => onChange(vector.map((current,i) => i === index ? featureScalar(event.target.value) : current === undefined ? '' : current))} />)}</div>{invalid && <small role="alert">请填写 {dimensions} 个坐标或表达式。</small>}</div>
}
