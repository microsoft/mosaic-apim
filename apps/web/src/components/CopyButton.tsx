import { Button } from '@fluentui/react-components'
import { CheckmarkRegular, CopyRegular } from '@fluentui/react-icons'
import { useState } from 'react'

/** A copy button. `label` names what it copies, mid-sentence, such as "base URL". */
export function CopyButton({ value, label }: { value: string; label: string }) {
  const [copied, setCopied] = useState(false)
  async function copy() {
    try {
      await navigator.clipboard?.writeText(value)
      setCopied(true)
    } catch {
      setCopied(false)
    }
  }
  return (
    <Button
      size="small"
      icon={copied ? <CheckmarkRegular /> : <CopyRegular />}
      aria-label={copied ? `Copied ${label}` : `Copy ${label}`}
      onClick={() => void copy()}
    >
      {copied ? 'Copied' : 'Copy'}
    </Button>
  )
}
