import type { ModelPoolType } from '../types'

const member = { width: 38, height: 12, rx: 3, fill: 'none', stroke: 'currentColor', strokeWidth: 1.25 }

/** A small picture of how a pool type spreads requests, beside its description. */
export function PoolTypeDiagram({ type, className }: { type: ModelPoolType; className?: string }) {
  return (
    <svg className={className} viewBox="0 0 180 56" aria-hidden="true" focusable="false">
      <circle cx="14" cy="28" r="6" fill="currentColor" />
      {type === 'breaker' && (
        <>
          <line x1="20" y1="28" x2="128" y2="10" stroke="currentColor" strokeWidth="3" />
          <line x1="20" y1="28" x2="128" y2="28" stroke="currentColor" strokeWidth="2" />
          <line x1="20" y1="28" x2="128" y2="46" stroke="currentColor" strokeWidth="1" />
          <rect x="128" y="4" {...member} />
          <rect x="128" y="22" {...member} />
          <rect x="128" y="40" {...member} />
        </>
      )}
      {type === 'preferential' && (
        <>
          <line x1="20" y1="28" x2="128" y2="10" stroke="currentColor" strokeWidth="2.5" />
          <line x1="20" y1="28" x2="128" y2="28" stroke="currentColor" strokeWidth="2.5" />
          <line x1="20" y1="28" x2="128" y2="46" stroke="currentColor" strokeWidth="1.25" strokeDasharray="4 3" />
          <rect x="128" y="4" {...member} fill="currentColor" fillOpacity="0.2" />
          <rect x="128" y="22" {...member} fill="currentColor" fillOpacity="0.2" />
          <rect x="128" y="40" {...member} strokeDasharray="4 3" />
        </>
      )}
      {type === 'linear' && (
        <>
          <path d="M20 28 H32 M28 24 L32 28 L28 32" fill="none" stroke="currentColor" strokeWidth="1.5" />
          <rect x="34" y="22" {...member} />
          <path d="M72 28 H84 M80 24 L84 28 L80 32" fill="none" stroke="currentColor" strokeWidth="1.5" />
          <rect x="86" y="22" {...member} />
          <path d="M124 28 H136 M132 24 L136 28 L132 32" fill="none" stroke="currentColor" strokeWidth="1.5" />
          <rect x="138" y="22" {...member} />
        </>
      )}
    </svg>
  )
}
