import { useCallback, useEffect, useRef } from 'react'
import { useLocation, useSearchParams } from 'react-router-dom'

export const WORKSPACE_MODES = ['new', 'edit', 'import', 'publish', 'review'] as const
export type WorkspaceMode = typeof WORKSPACE_MODES[number]

export interface WorkbenchLocationOptions {
  /** Stable section key (usually the active `tab`). Defaults to the URL tab. */
  section?: string
  selectedParameter?: string
  modeParameter?: string
  scopeParameter?: string
}

/** URLs contain resource identifiers and view state only, never drafts or credentials. */
export function useWorkbenchLocation({
  section, selectedParameter = 'selected', modeParameter = 'mode', scopeParameter = 'workbench',
}: WorkbenchLocationOptions = {}) {
  const [params, setParams] = useSearchParams()
  const location = useLocation()
  const scope = `${location.pathname}:${section ?? params.get('tab') ?? ''}`
  const recordedScope = params.get(scopeParameter)
  const staleScope = recordedScope !== null && recordedScope !== scope
  const requestedMode = params.get(modeParameter)
  const mode: WorkspaceMode | null = !staleScope && WORKSPACE_MODES.some((value) => value === requestedMode)
    ? requestedMode as WorkspaceMode : null
  const selectedId = staleScope ? null : params.get(selectedParameter)
  const returnScroll = useRef<{ scope: string; left: number; top: number } | null>(null)

  useEffect(() => {
    if (mode !== null || !returnScroll.current) return
    const previous = returnScroll.current
    returnScroll.current = null
    if (previous.scope !== scope) return
    const frame = requestAnimationFrame(() => window.scrollTo({ left: previous.left, top: previous.top }))
    return () => cancelAnimationFrame(frame)
  }, [mode, scope])

  useEffect(() => {
    if (!staleScope && recordedScope === null && (selectedId !== null || mode !== null)) {
      setParams((current) => {
        const next = new URLSearchParams(current)
        next.set(scopeParameter, scope)
        return next
      }, { replace: true, preventScrollReset: true })
      return
    }
    if (!staleScope && (requestedMode === null || mode !== null)) return
    setParams((current) => {
      const next = new URLSearchParams(current)
      if (staleScope) {
        next.delete(selectedParameter)
        next.delete(scopeParameter)
        next.delete('runId')
        next.delete('syncRun')
      }
      next.delete(modeParameter)
      return next
    }, { replace: true, preventScrollReset: true })
  }, [staleScope, recordedScope, selectedId, requestedMode, mode, selectedParameter, modeParameter, scopeParameter, scope, setParams])

  const select = useCallback((id: string | null) => {
    setParams((current) => {
      const next = new URLSearchParams(current)
      if (id !== current.get(selectedParameter)) {
        next.delete('runId')
        next.delete('syncRun')
      }
      if (id !== null) { next.set(selectedParameter, id); next.set(scopeParameter, scope) }
      else { next.delete(selectedParameter); next.delete(scopeParameter) }
      next.delete(modeParameter)
      return next
    }, { preventScrollReset: true })
  }, [setParams, selectedParameter, modeParameter, scopeParameter, scope])

  const open = useCallback((nextMode: WorkspaceMode, id?: string | null) => {
    if (mode === null) returnScroll.current = { scope, left: window.scrollX, top: window.scrollY }
    setParams((current) => {
      const next = new URLSearchParams(current)
      next.set(modeParameter, nextMode)
      next.set(scopeParameter, scope)
      if (id === null) next.delete(selectedParameter)
      else if (id !== undefined) next.set(selectedParameter, id)
      return next
    }, { preventScrollReset: true })
  }, [setParams, selectedParameter, modeParameter, scopeParameter, scope, mode])

  const close = useCallback((savedId?: string | null) => {
    setParams((current) => {
      const next = new URLSearchParams(current)
      next.delete(modeParameter)
      if (savedId === null) next.delete(selectedParameter)
      else if (savedId !== undefined) next.set(selectedParameter, savedId)
      if (next.has(selectedParameter)) next.set(scopeParameter, scope)
      else next.delete(scopeParameter)
      return next
    }, { replace: true, preventScrollReset: true })
  }, [setParams, selectedParameter, modeParameter, scopeParameter, scope])

  const setMode = useCallback((nextMode: WorkspaceMode | null) => {
    if (nextMode === null) close()
    else open(nextMode)
  }, [close, open])

  return { selectedId, mode, select, open, close, setMode }
}
