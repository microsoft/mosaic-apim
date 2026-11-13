import type { Principal } from './types'

interface RequesterRecord {
  requesterObjectId: string
  requesterPrincipalId?: string | null
  requesterName?: string | null
  requesterUsername?: string | null
}

export interface RequesterDisplay {
  /** A human-readable label when known, otherwise the technical ID for outcome messages. */
  label: string
  objectId: string
  /** Null until the principal registry has been read; a recorded principal ID is not proof. */
  registered: boolean | null
  displayName?: string
  savedLabel?: string | null
  capturedName?: string | null
  username?: string | null
}

export function requestingPrincipal(request: RequesterRecord, principals: Principal[] = []) {
  const objectId = request.requesterObjectId.toLowerCase()
  return principals.find(
    (principal) => principal.id === request.requesterPrincipalId || principal.objectId.toLowerCase() === objectId,
  )
}

export function requesterDisplay(request: RequesterRecord, principals?: Principal[]): RequesterDisplay {
  const principal = requestingPrincipal(request, principals)
  const savedLabel = principal?.label?.trim() || null
  const capturedName = request.requesterName?.trim() || null
  const username = request.requesterUsername?.trim() || null
  const displayName = savedLabel || capturedName || username || undefined
  return {
    label: displayName || request.requesterObjectId,
    objectId: request.requesterObjectId,
    registered: principals === undefined ? null : Boolean(principal),
    displayName,
    savedLabel,
    capturedName,
    username,
  }
}
