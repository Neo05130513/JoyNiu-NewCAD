import './joyniu-brand.css'

export default function JoyNiuBrand({ compact = false, editor = false }) {
  return <span className={`joyniu-brand${editor ? ' joyniu-brand--editor' : ''}`}>
    <span className="joyniu-brand__mark" aria-hidden="true"><i /></span>
    {!compact && <span className="joyniu-brand__copy">
      <strong>JoyNiu <em>CAD</em></strong>
      {!editor && <span className="joyniu-brand__tagline">创模 AI · ENGINEERING</span>}
    </span>}
  </span>
}
