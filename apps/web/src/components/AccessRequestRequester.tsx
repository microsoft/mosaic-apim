import { Text } from '@fluentui/react-components'
import type { RequesterDisplay } from '../requester-display'
import styles from '../pages/EntitlementsPage.module.css'

export function AccessRequestRequester({ requester, showRegistrationEffect = false, registrationPending = false }: {
  requester: RequesterDisplay
  showRegistrationEffect?: boolean
  registrationPending?: boolean
}) {
  const labelIsObjectId = requester.label.toLowerCase() === requester.objectId.toLowerCase()
  const savedLabel = requester.savedLabel !== undefined
    ? requester.savedLabel
    : requester.registered === true && !labelIsObjectId ? requester.label : null
  const capturedName = requester.capturedName?.trim()
  const username = requester.username?.trim()
  const hasSnapshot = Boolean(capturedName || username)
  const visibleLabel = !labelIsObjectId && (savedLabel || (requester.registered === false && hasSnapshot ? requester.label : null))
  const usernameIsLabel = Boolean(visibleLabel && username === visibleLabel)
  const identity = <>
    <Text className={styles.requesterCaption}>Object ID</Text>
    <Text className={styles.codeValue}><bdi dir="ltr" className="mono">{requester.objectId}</bdi></Text>
  </>
  return <section className={styles.requesterIdentity} aria-label="Requester identity">
    {requester.registered === null ? <Text size={200} role="status">
      {registrationPending ? 'Checking MOSAIC registration...' : 'MOSAIC registration status unavailable.'}
    </Text> : savedLabel ? <>
      <Text className={styles.requesterCaption}>MOSAIC label</Text>
      {labelIsObjectId
        ? <Text size={200}>Uses the object ID shown below.</Text>
        : <Text className={styles.primaryCell}><bdi>{savedLabel}</bdi></Text>}
    </> : requester.registered
      ? <Text size={200}>Registered in MOSAIC; no saved label.</Text>
      : hasSnapshot ? <>
        <Text className={styles.requesterCaption}>Label for new registration</Text>
        {labelIsObjectId
          ? <Text size={200}>Uses the object ID shown below.</Text>
          : <Text className={styles.primaryCell}><bdi>{requester.label}</bdi></Text>}
      </> : <Text className={styles.primaryCell}>Requester name unavailable</Text>}
    {hasSnapshot ? <section className={styles.requesterSnapshot} aria-label="Sign-in identity at request time">
      <Text className={styles.requesterCaption}>Sign-in identity at request time</Text>
      {capturedName && <Text className={styles.primaryCell}><bdi>{capturedName}</bdi></Text>}
      {username && username !== capturedName && !usernameIsLabel &&
        <Text><bdi>{username}</bdi></Text>}
      {!capturedName && usernameIsLabel &&
        <Text size={200}>The captured username is the label above.</Text>}
      {identity}
    </section> : <>
      <Text size={200}>No sign-in display details were recorded.</Text>
      {identity}
    </>}
    {showRegistrationEffect && requester.registered === false && <Text size={200}>
      Not registered in MOSAIC yet. Approving registers them as a user principal.
    </Text>}
  </section>
}
