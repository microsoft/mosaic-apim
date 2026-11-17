import { useAnnounce, type AnnounceContextValue } from '@fluentui/react-components'
import { type RefObject, useMemo } from 'react'

export function useFocusAwareAnnounce(focusTargetRef: RefObject<HTMLElement | null>): AnnounceContextValue {
  const { announce } = useAnnounce()
  return useMemo(() => ({
    announce: (message, options) => {
      const target = focusTargetRef.current
      if (target && target.ownerDocument.activeElement === target) return
      announce(message, options)
    },
  }), [announce, focusTargetRef])
}
