import {
  Button,
  Card,
  Dialog,
  DialogActions,
  DialogBody,
  DialogContent,
  DialogSurface,
  DialogTitle,
  MessageBar,
  MessageBarBody,
  MessageBarTitle,
  Radio,
  RadioGroup,
  Text,
  Title3,
  useId,
  useRestoreFocusTarget,
} from '@fluentui/react-components'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { type Ref, useEffect, useRef, useState } from 'react'
import { useMosaicApi } from '../api'
import { MANAGEMENT_MODE_LABELS } from '../labels'
import type { Gateway, ManagementMode, Publication } from '../types'
import styles from './GatewayManagementMode.module.css'

const MODES: ManagementMode[] = ['observe', 'manage']

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : 'An unexpected error occurred.'
}

function lastCheck(checkedAt: string | null | undefined): string {
  return checkedAt
    ? `the last access check (${new Date(checkedAt).toLocaleString()})`
    : 'the last access check'
}

/** Publications that still have something MOSAIC created in API Management. */
function publishedModels(publications: Publication[]): Publication[] {
  return publications.filter(
    (publication) =>
      publication.status === 'published' ||
      publication.resources.some((resource) => resource.createdByMosaic),
  )
}

function WriteAccessWarning({
  gateway,
  reasonId,
  checking,
  onCheck,
  containerRef,
}: {
  gateway: Gateway
  reasonId: string
  checking: boolean
  onCheck: () => void
  containerRef: Ref<HTMLDivElement>
}) {
  const { access } = gateway
  const managed = gateway.managementMode === 'manage'
  const { remediation } = access

  return (
    <div className={styles.warning} ref={containerRef}>
      <MessageBar intent="warning">
        <MessageBarBody>
          <MessageBarTitle>
            {managed ? 'MOSAIC refuses to publish to this gateway' : 'MOSAIC can’t manage this gateway yet'}
          </MessageBarTitle>
          <span id={reasonId}>
            {managed
              ? `Write access isn’t confirmed by ${lastCheck(access.checkedAt)}, so MOSAIC refuses to plan, apply or unpublish models on this gateway. You can still switch it to Observe.`
              : `Manage mode needs write access, and ${lastCheck(access.checkedAt)} didn’t confirm it.`}
          </span>
        </MessageBarBody>
      </MessageBar>
      {remediation ? (
        <>
          <Text block>
            {access.canRead
              ? 'Grant this role, then check access again:'
              : 'MOSAIC can’t read this gateway either, so the access check asks for read access first. Grant this role, then check access again:'}
          </Text>
          <dl className={styles.facts}>
            <dt>Role</dt>
            <dd>{remediation.roleName}</dd>
            <dt>Scope</dt>
            <dd>
              <code className={styles.scope}>{remediation.scope}</code>
            </dd>
            <dt>Assign to</dt>
            <dd>
              MOSAIC’s managed identity
              {remediation.principalId ? ` (${remediation.principalId})` : ''}
            </dd>
          </dl>
        </>
      ) : (
        <Text block>
          {access.message ?? 'The access check did not finish.'} Check access again once MOSAIC can
          reach the gateway.
        </Text>
      )}
      <Button appearance="secondary" disabledFocusable={checking} onClick={onCheck}>
        {checking ? 'Checking access…' : 'Check access'}
      </Button>
    </div>
  )
}

function ManageConsequences({ gateway, textId }: { gateway: Gateway; textId: string }) {
  return (
    <>
      <Text block id={textId}>
        Switching changes nothing in {gateway.serviceName} by itself. It lets administrators publish
        models to this gateway. MOSAIC then writes to it only when an administrator applies a
        reviewed publish plan, unpublishes a model, or confirms recovery of an interrupted apply.
      </Text>
      <Text block>
        Applying a model’s plan creates these resources, or updates them if they already exist:
      </Text>
      <ul className={styles.list}>
        <li>a backend that points at the model endpoint</li>
        <li>a policy fragment that routes calls to that backend and enforces the model’s token limit</li>
        <li>an API for the model, with its operations and an API policy that includes the fragment</li>
        <li>a product that carries the API, linked to it</li>
        <li>
          a subscription to the product when the model requires one or, with governed access, one
          API-scoped subscription for each direct grant
        </li>
      </ul>
      <Text block>
        MOSAIC won’t replace an API, API policy or policy fragment it didn’t create, and won’t publish
        at a path another API already serves. A backend, product or subscription that already exists
        with a planned name shows in the plan as an update and is replaced when the plan is applied;
        with governed access, MOSAIC refuses instead. Rollback and unpublish remove only what MOSAIC
        created and don’t restore anything it replaced. Other APIs, the global policy, named values,
        users and groups are never changed.
      </Text>
    </>
  )
}

function ObserveConsequences({
  gateway,
  published,
  textId,
}: {
  gateway: Gateway
  published: Publication[] | undefined
  textId: string
}) {
  const one = published?.length === 1

  return (
    <>
      <Text block id={textId}>
        Switching changes nothing in {gateway.serviceName}. MOSAIC stops writing to it and refuses to
        plan, apply or unpublish models on this gateway until you switch back to Manage.
      </Text>
      {published === undefined && (
        <Text block>
          Models already published through this gateway stay in API Management and keep handling
          calls as they do now.
        </Text>
      )}
      {published?.length === 0 && <Text block>No models are published through this gateway.</Text>}
      {published !== undefined && published.length > 0 && (
        <>
          <Text block>
            {one
              ? 'This model was published through this gateway. It stays in API Management and keeps handling calls as it does now:'
              : 'These models were published through this gateway. They stay in API Management and keep handling calls as they do now:'}
          </Text>
          <ul className={styles.list} aria-label="Models published through this gateway">
            {published.map((publication) => (
              <li key={publication.id}>
                {publication.displayName} (/{publication.apiPath})
              </li>
            ))}
          </ul>
        </>
      )}
      {published?.length !== 0 && (
        <Text block>
          Grant and access changes saved in MOSAIC for {one ? 'this model' : 'these models'},
          including disabling a grant to revoke it, won’t reach API Management until the gateway is
          managed again and the model’s plan is applied. To remove a model from API Management,
          unpublish it before you switch.
        </Text>
      )}
    </>
  )
}

/**
 * Shows and changes whether MOSAIC may write to a gateway. The API refuses manage mode until the
 * access check confirms write access, and switching either way changes nothing in APIM itself.
 */
export function GatewayManagementMode({ gateway }: { gateway: Gateway }) {
  const api = useMosaicApi()
  const queryClient = useQueryClient()
  const headingId = useId('management-mode-')
  const descriptionId = useId('management-mode-description-')
  const reasonId = useId('management-mode-reason-')
  const noticeId = useId('management-mode-notice-')
  const dialogTextId = useId('management-mode-dialog-')
  // The dialog opens from a radio rather than a DialogTrigger, so mark the radios as where focus
  // returns when it closes.
  const restoreFocus = useRestoreFocusTarget()
  const manageRadio = useRef<HTMLInputElement>(null)
  const warning = useRef<HTMLDivElement>(null)
  const refocus = useRef(false)
  const [target, setTarget] = useState<ManagementMode>('manage')
  const [confirming, setConfirming] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)

  const publications = useQuery({
    queryKey: ['publications', gateway.id],
    queryFn: () => api.listPublications(gateway.id),
  })

  function record(updated: Gateway) {
    queryClient.setQueryData(['gateway', updated.id], updated)
    queryClient.setQueryData<Gateway[]>(['gateways'], (current) =>
      current?.map((item) => (item.id === updated.id ? updated : item)),
    )
    void queryClient.invalidateQueries({ queryKey: ['gateway', updated.id] })
    void queryClient.invalidateQueries({ queryKey: ['gateways'] })
  }

  const change = useMutation({
    mutationFn: (managementMode: ManagementMode) =>
      api.updateGateway(gateway.id, { managementMode }),
    onSuccess: (updated) => {
      record(updated)
      setConfirming(false)
      setNotice(
        updated.managementMode === 'manage'
          ? 'Switched to manage mode. Nothing in API Management changed. Models can now be published to this gateway.'
          : 'Switched to observe mode. Nothing in API Management changed. MOSAIC won’t plan, apply or unpublish models here until you switch back to Manage.',
      )
    },
    onError: () => {
      // A refusal can mean this page's copy of the gateway, such as its access, is out of date.
      void queryClient.invalidateQueries({ queryKey: ['gateway', gateway.id] })
    },
  })

  const recheck = useMutation({
    mutationFn: () => api.preflightGateway(gateway.id),
    onSuccess: (checked) => {
      // Confirmed write access removes the warning, and the Check access button with it. Move
      // focus to the control it unlocked, unless the administrator has already moved on.
      const active = document.activeElement
      refocus.current =
        checked.access.canWrite &&
        (active === null || active === document.body || !!warning.current?.contains(active))
      record(checked)
      if (!checked.access.canWrite) {
        setNotice(null)
      } else {
        setNotice(
          checked.managementMode === 'manage'
            ? 'Write access confirmed. MOSAIC can publish to this gateway again.'
            : 'Write access confirmed. You can switch this gateway to Manage.',
        )
      }
    },
  })

  function requestMode(mode: ManagementMode) {
    if (mode === gateway.managementMode) {
      return
    }
    change.reset()
    setNotice(null)
    setTarget(mode)
    setConfirming(true)
  }

  const { access, managementMode: mode } = gateway

  useEffect(() => {
    if (access.canWrite && refocus.current) {
      refocus.current = false
      manageRadio.current?.focus()
    }
  }, [access.canWrite])

  const locked = mode === 'observe' && !access.canWrite
  const published = publications.data ? publishedModels(publications.data) : undefined
  const applying = (publications.data ?? []).filter(
    (publication) => publication.status === 'applying',
  )
  const heldBack = published?.length ?? 0
  const writes =
    'writes to API Management only when an administrator applies a reviewed publish plan, unpublishes a model, or confirms recovery of an interrupted apply.'
  const description =
    mode === 'manage'
      ? access.canWrite
        ? `This gateway is in manage mode. MOSAIC can publish models to it, and ${writes}`
        : `This gateway is in manage mode. MOSAIC ${writes}`
      : `This gateway is in observe mode. MOSAIC reads it and never changes it, so models can’t be published to it.${
          heldBack === 1
            ? ' One model published earlier still runs in API Management, but MOSAIC can’t update or unpublish it until you switch to Manage.'
            : heldBack > 1
              ? ` ${heldBack} models published earlier still run in API Management, but MOSAIC can’t update or unpublish them until you switch to Manage.`
              : ''
        }`

  return (
    <Card className={styles.card}>
      <div className={styles.header}>
        <Title3 as="h2" id={headingId} className={styles.heading}>
          Management mode
        </Title3>
        <Text id={descriptionId} className={styles.muted}>
          {description}
        </Text>
      </div>
      <RadioGroup
        aria-labelledby={headingId}
        aria-describedby={[descriptionId, !access.canWrite && reasonId, notice && noticeId]
          .filter(Boolean)
          .join(' ')}
        layout="horizontal"
        value={mode}
        disabled={locked}
        onChange={(_, data) => requestMode(data.value as ManagementMode)}
      >
        {MODES.map((value) => (
          <Radio
            key={value}
            ref={value === 'manage' ? manageRadio : undefined}
            value={value}
            label={MANAGEMENT_MODE_LABELS[value]}
            {...restoreFocus}
          />
        ))}
      </RadioGroup>
      {!access.canWrite && (
        <WriteAccessWarning
          gateway={gateway}
          reasonId={reasonId}
          checking={recheck.isPending}
          onCheck={() => recheck.mutate()}
          containerRef={warning}
        />
      )}
      {recheck.isError && (
        <MessageBar intent="error">
          <MessageBarBody>
            <MessageBarTitle>The access check failed</MessageBarTitle>
            {errorMessage(recheck.error)}
          </MessageBarBody>
        </MessageBar>
      )}
      {notice && (
        <MessageBar intent="success">
          <MessageBarBody id={noticeId}>{notice}</MessageBarBody>
        </MessageBar>
      )}

      <Dialog
        modalType="alert"
        open={confirming}
        onOpenChange={(_, data) => {
          if (!data.open && !change.isPending) {
            setConfirming(false)
          }
        }}
      >
        <DialogSurface aria-describedby={dialogTextId}>
          <DialogBody>
            <DialogTitle>
              {target === 'manage' ? 'Switch to manage mode?' : 'Switch to observe mode?'}
            </DialogTitle>
            <DialogContent className={styles.dialogContent}>
              {target === 'manage' ? (
                <ManageConsequences gateway={gateway} textId={dialogTextId} />
              ) : (
                <ObserveConsequences gateway={gateway} published={published} textId={dialogTextId} />
              )}
              {applying.length > 0 && (
                <MessageBar intent="warning">
                  <MessageBarBody>
                    {applying.map((publication) => publication.displayName).join(', ')}{' '}
                    {applying.length === 1 ? 'is' : 'are'} being published or unpublished. MOSAIC
                    refuses to change the management mode until that run finishes, or is recovered
                    if it was interrupted.
                  </MessageBarBody>
                </MessageBar>
              )}
              {change.isError && (
                <MessageBar intent="error" role="alert">
                  <MessageBarBody>
                    <MessageBarTitle>The management mode was not changed</MessageBarTitle>
                    {errorMessage(change.error)}
                  </MessageBarBody>
                </MessageBar>
              )}
            </DialogContent>
            <DialogActions>
              <Button
                appearance="secondary"
                disabled={change.isPending}
                onClick={() => setConfirming(false)}
              >
                Cancel
              </Button>
              <Button
                appearance="primary"
                disabledFocusable={change.isPending}
                onClick={() => change.mutate(target)}
              >
                {target === 'manage' ? 'Switch to manage' : 'Switch to observe'}
              </Button>
            </DialogActions>
          </DialogBody>
        </DialogSurface>
      </Dialog>
    </Card>
  )
}
