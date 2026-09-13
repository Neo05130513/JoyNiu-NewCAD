import { useEffect, useState } from 'react'

// Switching tools must not throw away an unfinished drawing, assembly or
// parameter form. The parent keys this boundary by authenticated account.
export default function RetainedWorkspace({ active, children }) {
  const [visited, setVisited] = useState(active)
  useEffect(() => { if (active) setVisited(true) }, [active])
  if (!visited && !active) return null
  return <div className="retained-workspace" hidden={!active}>{children}</div>
}
