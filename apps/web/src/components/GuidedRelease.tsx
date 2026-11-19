import type { ReactNode } from 'react'
import './GuidedRelease.css'

export type ReleaseStage = 'register' | 'review' | 'apply' | 'connect'
const stages: { id: ReleaseStage; title: string; detail: string }[] = [
  { id: 'register', title: 'Register', detail: 'Save endpoint configuration' },
  { id: 'review', title: 'Review', detail: 'Inspect the publication plan' },
  { id: 'apply', title: 'Apply publication', detail: 'Change the gateway explicitly' },
  { id: 'connect', title: 'Grant and connect', detail: 'Apply access separately' },
]

export function GuidedReleaseProgress({ stage }: { stage: ReleaseStage }) {
  return <nav aria-label="Guided release"><ol className="guided-release-steps">
    {stages.map(({ id, title, detail }, index) => <li key={id}
      aria-current={id === stage ? 'step' : undefined}>
      <span className="guided-release-number" aria-hidden="true">{index + 1}</span>
      <span><strong>{title}</strong><span>{detail}</span></span>
    </li>)}
  </ol></nav>
}

export function GuidedRelease({ enabled, stage, guidance, children }: {
  enabled: boolean
  stage: ReleaseStage
  guidance: ReactNode
  children: ReactNode
}) {
  if (!enabled) return children
  return <div className="guided-release">
    <GuidedReleaseProgress stage={stage} />
    <div className="guided-release-layout">
      <div className="guided-release-task">{children}</div>
      <aside className="guided-release-consequences" aria-label="Release boundaries">
        {guidance}
      </aside>
    </div>
  </div>
}
