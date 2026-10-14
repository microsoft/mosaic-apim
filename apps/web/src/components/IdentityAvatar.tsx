import { useMsal } from '@azure/msal-react'
import { useQuery } from '@tanstack/react-query'
import { BotRegular, PeopleTeamRegular, PersonRegular, AppGenericRegular, ShieldPersonRegular } from '@fluentui/react-icons'
import { useEffect, useRef, useState } from 'react'
import { useMosaicApi } from '../api'
import { initialsFor } from '../initials'
import { runtimeConfig } from '../runtime-config'
import type { PrincipalKind } from '../types'

const icons = {
  user: <PersonRegular />,
  agentUser: <BotRegular />,
  agentIdentity: <BotRegular />,
  servicePrincipal: <AppGenericRegular />,
  managedIdentity: <ShieldPersonRegular />,
  securityGroup: <PeopleTeamRegular />,
}

interface IdentityAvatarProps {
  name: string
  kind?: PrincipalKind
  objectId?: string
  size?: number
}

export function IdentityAvatar({ name, kind = 'user', objectId, size = 36 }: IdentityAvatarProps) {
  const photoSupported = kind === 'user' && Boolean(objectId?.match(/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i))
  if (photoSupported) return <PhotoAvatar name={name} objectId={objectId!} size={size} />
  return (
    <span className={`identity-avatar identity-avatar-${kind}`} style={{ width: size, height: size }} aria-hidden="true" data-photo-state="fallback">
      {kind === 'user' ? initialsFor(name) : icons[kind]}
    </span>
  )
}

function PhotoAvatar({ name, objectId, size }: { name: string; objectId: string; size: number }) {
  const api = useMosaicApi()
  const { accounts } = useMsal()
  const ref = useRef<HTMLSpanElement>(null)
  const [visible, setVisible] = useState(false)
  const [image, setImage] = useState<{ blob: Blob; url: string } | null>(null)
  const [failedImage, setFailedImage] = useState<Blob | null>(null)
  useEffect(() => {
    if (typeof IntersectionObserver === 'undefined') {
      setVisible(true)
      return
    }
    const observer = new IntersectionObserver(([entry]) => {
      if (entry.isIntersecting) {
        setVisible(true)
        observer.disconnect()
      }
    })
    if (ref.current) observer.observe(ref.current)
    return () => observer.disconnect()
  }, [])
  const photo = useQuery({
    queryKey: ['directory-photo', accounts[0]?.tenantId ?? runtimeConfig.entraTenantId, accounts[0]?.homeAccountId ?? 'local', objectId],
    queryFn: () => api.getDirectoryPhoto(objectId!),
    enabled: visible,
    staleTime: 300_000,
    gcTime: 300_000,
    retry: false,
    refetchOnWindowFocus: false,
  })
  useEffect(() => {
    if (!photo.data) return
    const url = URL.createObjectURL(photo.data)
    setImage({ blob: photo.data, url })
    return () => URL.revokeObjectURL(url)
  }, [photo.data])
  const imageError = photo.data !== undefined && failedImage === photo.data
  const url = !imageError && image?.blob === photo.data ? image?.url : undefined
  return (
    <span ref={ref} className="identity-avatar identity-avatar-user" style={{ width: size, height: size }}
      aria-hidden="true" data-photo-state={photo.isError || imageError ? 'unavailable' : url ? 'loaded' : 'fallback'}
      title={photo.isError || imageError ? 'Profile photo unavailable; using an identity fallback' : undefined}>
      {url ? <img src={url} alt="" onError={() => setFailedImage(photo.data ?? null)} /> : initialsFor(name)}
    </span>
  )
}
