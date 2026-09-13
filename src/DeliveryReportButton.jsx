import { useState } from 'react'
import { cadDeliveryReportAvailability, downloadCadDeliveryReport } from './cadDeliveryReport.js'

export default function DeliveryReportButton({ model, generation, drawingJob, confirmedBy, confirmedAt, busy = false, onError, className = 'secondary-button' }) {
  const [error, setError] = useState('')
  const availability = cadDeliveryReportAvailability({ model, generation, drawingJob })
  const download = () => {
    setError('')
    try { downloadCadDeliveryReport({ model, generation, drawingJob, confirmedBy, confirmedAt }) }
    catch (failure) { setError(failure.message); onError?.(failure) }
  }
  return <><button type="button" className={className} disabled={busy || !availability.allowed} title={availability.reason || '下载当前已确认模型的 HTML 核对报告'} onClick={download}>导出核对报告</button>{error && <p role="alert">{error}</p>}</>
}
