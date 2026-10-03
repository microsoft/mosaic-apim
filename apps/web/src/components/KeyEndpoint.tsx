import {
  Button,
  Card,
  Dialog,
  DialogActions,
  DialogBody,
  DialogContent,
  DialogSurface,
  DialogTitle,
  Field,
  Input,
  Select,
  Table,
  TableBody,
  TableCell,
  TableHeader,
  TableHeaderCell,
  TableRow,
  Text,
  Title3,
  useRestoreFocusTarget,
} from '@fluentui/react-components'
import { AddRegular, DismissRegular } from '@fluentui/react-icons'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { type FormEvent, useEffect, useRef, useState } from 'react'
import { ApiError, useMosaicApi } from '../api'
import { EmptyState, ErrorState } from './AsyncState'
import {
  API_SHAPE_LABELS,
  API_SHAPE_SHORT_LABELS,
  bedrockModelName,
  blankDeclaration,
  effectiveShape,
  shapesFor,
  suggestedShape,
  type DeclarationDraft,
} from '../key-endpoint'
import type { ApiShape, ModelEndpoint, ModelProvider } from '../types'
import styles from './KeyEndpoint.module.css'

/**
 * The deployments to declare while registering a key-authenticated endpoint. An API key can't list
 * a resource's deployments, so the administrator names the ones to publish. On AWS Bedrock they're
 * model IDs, and the API is always Anthropic Messages.
 */
export function DeclarationFields({
  drafts,
  provider,
  onChange,
}: {
  drafts: DeclarationDraft[]
  provider: ModelProvider | null
  onChange: (drafts: DeclarationDraft[]) => void
}) {
  const shapes = shapesFor(provider)
  const bedrock = provider === 'awsBedrock'

  function update(key: number, change: Partial<DeclarationDraft>) {
    onChange(
      drafts.map((draft) => {
        if (draft.key !== key) return draft
        const next = { ...draft, ...change }
        if (change.modelName !== undefined && !next.shapeChosen) {
          next.apiShape = suggestedShape(next.modelName, provider)
        }
        // A Bedrock model ID names the Claude model it serves, so that model is offered until the
        // administrator types another.
        if (
          bedrock &&
          change.deploymentName !== undefined &&
          draft.modelName === bedrockModelName(draft.deploymentName)
        ) {
          next.modelName = bedrockModelName(next.deploymentName)
        }
        return next
      }),
    )
  }

  function removeDraft(key: number) {
    onChange(drafts.filter((item) => item.key !== key))
  }

  return (
    <fieldset className={styles.declarations}>
      <legend>{bedrock ? 'Models to pool' : 'Deployments to publish'}</legend>
      {bedrock ? (
        <Text size={200} className={styles.muted}>
          A Bedrock API key can&apos;t list models, so name the model IDs to pool. Give each the
          model it serves as Azure names it, such as claude-sonnet-4-5, so it pools beside the same
          model on Azure. You can declare more later.
        </Text>
      ) : (
        <Text size={200} className={styles.muted}>
          An API key can&apos;t list a resource&apos;s deployments, so name the ones to publish and
          the API each one takes. You can declare more later.
        </Text>
      )}
      {drafts.length > 0 &&
        (bedrock ? (
          <div className={styles.bedrockHeader} aria-hidden="true">
            <span>Model ID</span>
            <span>Model</span>
            <span>Remove</span>
          </div>
        ) : (
          <div className={styles.declarationHeader} aria-hidden="true">
            <span>Deployment name</span>
            <span>Model</span>
            <span>API</span>
            <span>Remove</span>
          </div>
        ))}
      {drafts.map((draft, index) =>
        bedrock ? (
          <div key={draft.key} className={styles.bedrockRow}>
            <Input
              aria-label={`Model ${index + 1} ID`}
              value={draft.deploymentName}
              placeholder="us.anthropic.claude-sonnet-4-5-20250929-v1:0"
              spellCheck={false}
              onChange={(_, data) => update(draft.key, { deploymentName: data.value })}
            />
            <Input
              aria-label={`Model ${index + 1} name`}
              value={draft.modelName}
              placeholder="claude-sonnet-4-5"
              spellCheck={false}
              onChange={(_, data) => update(draft.key, { modelName: data.value })}
            />
            <Button
              appearance="subtle"
              icon={<DismissRegular />}
              aria-label={`Remove model ${index + 1}`}
              title="Remove"
              onClick={() => removeDraft(draft.key)}
            />
          </div>
        ) : (
          <div key={draft.key} className={styles.declarationRow}>
            <Input
              aria-label={`Deployment ${index + 1} name`}
              value={draft.deploymentName}
              placeholder="claude-sonnet-4-5"
              onChange={(_, data) => update(draft.key, { deploymentName: data.value })}
            />
            <Input
              aria-label={`Deployment ${index + 1} model`}
              value={draft.modelName}
              placeholder="claude-sonnet-4-5"
              onChange={(_, data) => update(draft.key, { modelName: data.value })}
            />
            <Select
              aria-label={`Deployment ${index + 1} API`}
              value={effectiveShape(draft, provider)}
              onChange={(event) =>
                update(draft.key, { apiShape: event.target.value as ApiShape, shapeChosen: true })
              }
            >
              {shapes.map((shape) => (
                <option key={shape} value={shape}>
                  {API_SHAPE_SHORT_LABELS[shape]}
                </option>
              ))}
            </Select>
            <Button
              appearance="subtle"
              icon={<DismissRegular />}
              aria-label={`Remove deployment ${index + 1}`}
              title="Remove"
              onClick={() => removeDraft(draft.key)}
            />
          </div>
        ),
      )}
      <div>
        <Button
          appearance="secondary"
          icon={<AddRegular />}
          onClick={() => onChange([...drafts, blankDeclaration(provider)])}
        >
          {bedrock ? 'Add a model' : 'Add a deployment'}
        </Button>
      </div>
    </fieldset>
  )
}

function useFocusOnChange<T>(value: T, active: boolean) {
  const ref = useRef<HTMLDivElement>(null)
  const shown = useRef(value)
  useEffect(() => {
    const previous = shown.current
    shown.current = value
    if (!active || !value || Object.is(value, previous)) return
    ref.current?.focus()
  }, [value, active])
  return ref
}

function DeclareDeploymentDialog({
  endpoint,
  open,
  onClose,
}: {
  endpoint: ModelEndpoint
  open: boolean
  onClose: () => void
}) {
  const api = useMosaicApi()
  const queryClient = useQueryClient()
  const bedrock = endpoint.provider === 'awsBedrock'
  const [deploymentName, setDeploymentName] = useState('')
  const [modelName, setModelName] = useState('')
  const [apiShape, setApiShape] = useState<ApiShape | null>(null)
  const shapes = shapesFor(endpoint.provider)
  const shape = apiShape && shapes.includes(apiShape) ? apiShape : suggestedShape(modelName, endpoint.provider)

  const declare = useMutation({
    mutationFn: () =>
      api.declareModelDeployment(endpoint.id, {
        deploymentName: deploymentName.trim(),
        modelName: modelName.trim(),
        apiShape: shape,
      }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['model-endpoints'] })
      await queryClient.invalidateQueries({ queryKey: ['publishable-models'] })
      await queryClient.invalidateQueries({ queryKey: ['model-pools'] })
      close()
    },
  })
  // A refusal says why at the top of the dialog, and takes focus so a screen reader reads it.
  const errorRef = useFocusOnChange(declare.error, open)

  function close() {
    setDeploymentName('')
    setModelName('')
    setApiShape(null)
    declare.reset()
    onClose()
  }

  function submit(event: FormEvent) {
    event.preventDefault()
    if (declare.isPending) return
    declare.mutate()
  }

  function changeModelId(value: string) {
    // The Claude model a Bedrock model ID serves is offered until the administrator types another.
    if (modelName === bedrockModelName(deploymentName)) setModelName(bedrockModelName(value))
    setDeploymentName(value)
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(_, data) => {
        if (!data.open && !declare.isPending) close()
      }}
    >
      <DialogSurface>
        <form onSubmit={submit}>
          <DialogBody>
            <DialogTitle>
              {`${bedrock ? 'Declare a model on' : 'Declare a deployment on'} ${endpoint.name}`}
            </DialogTitle>
            <DialogContent className={styles.form}>
              {declare.isError && (
                <div ref={errorRef} tabIndex={-1}>
                  <ErrorState
                    title={
                      bedrock
                        ? "MOSAIC didn't declare this model"
                        : "MOSAIC didn't declare this deployment"
                    }
                    error={declare.error}
                  />
                </div>
              )}
              {bedrock ? (
                <>
                  <Text size={200} className={styles.muted}>
                    Use the model ID or inference profile ID exactly as AWS names it. MOSAIC
                    can&apos;t check it with AWS, so a wrong ID shows up when a request through a
                    model pool reaches it.
                  </Text>
                  <Field label="Model ID" required>
                    <Input
                      value={deploymentName}
                      spellCheck={false}
                      onChange={(_, data) => changeModelId(data.value)}
                      placeholder="us.anthropic.claude-sonnet-4-5-20250929-v1:0"
                    />
                  </Field>
                  <Field
                    label="Model"
                    required
                    hint="The model as Azure names it, so it pools beside the same model on Azure."
                  >
                    <Input
                      value={modelName}
                      spellCheck={false}
                      onChange={(_, data) => setModelName(data.value)}
                      placeholder="claude-sonnet-4-5"
                    />
                  </Field>
                </>
              ) : (
                <>
                  <Text size={200} className={styles.muted}>
                    Use the deployment&apos;s name exactly as the resource shows it. MOSAIC
                    can&apos;t confirm it exists without calling the model, so a wrong name shows up
                    as a 404 when the published API is called.
                  </Text>
                  <Field label="Deployment name" required>
                    <Input
                      value={deploymentName}
                      onChange={(_, data) => setDeploymentName(data.value)}
                      placeholder="claude-sonnet-4-5"
                    />
                  </Field>
                  <Field label="Model" required>
                    <Input
                      value={modelName}
                      onChange={(_, data) => setModelName(data.value)}
                      placeholder="claude-sonnet-4-5"
                    />
                  </Field>
                  <Field label="API" hint="The API MOSAIC publishes this deployment through.">
                    <Select
                      value={shape}
                      onChange={(event) => setApiShape(event.target.value as ApiShape)}
                    >
                      {shapes.map((item) => (
                        <option key={item} value={item}>
                          {API_SHAPE_LABELS[item]}
                        </option>
                      ))}
                    </Select>
                  </Field>
                </>
              )}
            </DialogContent>
            <DialogActions>
              <Button appearance="secondary" disabledFocusable={declare.isPending} onClick={close}>
                Cancel
              </Button>
              <Button appearance="primary" type="submit" disabledFocusable={declare.isPending}>
                {declare.isPending ? 'Declaring…' : bedrock ? 'Declare model' : 'Declare deployment'}
              </Button>
            </DialogActions>
          </DialogBody>
        </form>
      </DialogSurface>
    </Dialog>
  )
}

/**
 * Replacing the API key MOSAIC keeps for an endpoint. The new key becomes the next version of the
 * same Key Vault secret, so nothing published needs a new plan.
 */
export function StoredKeyActions({
  endpoint,
  onReplaced,
}: {
  endpoint: ModelEndpoint
  onReplaced: (message: string) => void
}) {
  const restoreFocus = useRestoreFocusTarget()
  const [replacing, setReplacing] = useState(false)

  return (
    <div>
      <Button appearance="secondary" onClick={() => setReplacing(true)} {...restoreFocus}>
        Replace API key
      </Button>
      <ReplaceKeyDialog
        endpoint={endpoint}
        open={replacing}
        onClose={() => setReplacing(false)}
        onReplaced={onReplaced}
      />
    </div>
  )
}

function ReplaceKeyDialog({
  endpoint,
  open,
  onClose,
  onReplaced,
}: {
  endpoint: ModelEndpoint
  open: boolean
  onClose: () => void
  onReplaced: (message: string) => void
}) {
  const api = useMosaicApi()
  const queryClient = useQueryClient()
  const bedrock = endpoint.provider === 'awsBedrock'
  // Held only while the dialog is open, and cleared when it closes.
  const [apiKey, setApiKey] = useState('')
  const [touched, setTouched] = useState(false)

  const replace = useMutation({
    mutationFn: (key: string) => api.updateModelEndpoint(endpoint.id, { apiKey: key }),
    onSuccess: async (updated) => {
      await queryClient.invalidateQueries({ queryKey: ['model-endpoints'] })
      onReplaced(
        bedrock
          ? updated.status === 'pending' && updated.access.evaluation === 'notEvaluated'
            ? `Stored the new key for ${endpoint.name}. MOSAIC doesn't check keys with AWS, so ` +
              'the next request through a model pool tells whether AWS accepts it. API ' +
              'Management picks it up within four hours.'
            : `Stored the new key for ${endpoint.name}, but MOSAIC found a problem with it. Its ` +
              'Access card says what.'
          : updated.access.canRead
            ? `Stored the new key for ${endpoint.name}, and the endpoint accepts it. API ` +
              'Management picks it up within four hours.'
            : `Stored the new key for ${endpoint.name}, but MOSAIC couldn't confirm the endpoint ` +
              'accepts it. Its Access card says why.',
      )
      close()
    },
    onError: async (error) => {
      if (
        error instanceof ApiError &&
        error.body?.details?.reason === 'keyReplacedNotRecorded'
      ) {
        await queryClient.invalidateQueries({ queryKey: ['model-endpoints'] })
        onReplaced(error.body.message ?? error.message)
        close()
      }
    },
  })
  // A refusal says why at the top of the dialog, and takes focus so a screen reader reads it.
  const errorRef = useFocusOnChange(replace.error, open)

  function close() {
    setApiKey('')
    setTouched(false)
    replace.reset()
    onClose()
  }

  function submit(event: FormEvent) {
    event.preventDefault()
    if (replace.isPending) return
    setTouched(true)
    const key = apiKey.trim()
    if (!key) return
    replace.mutate(key)
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(_, data) => {
        if (!data.open && !replace.isPending) close()
      }}
    >
      <DialogSurface>
        <form onSubmit={submit}>
          <DialogBody>
            <DialogTitle>Replace the API key for {endpoint.name}</DialogTitle>
            <DialogContent className={styles.form}>
              {replace.isError && (
                <div ref={errorRef} tabIndex={-1}>
                  <ErrorState title="MOSAIC didn't replace the key" error={replace.error} />
                </div>
              )}
              {bedrock ? (
                <Text size={200} className={styles.muted}>
                  MOSAIC stores the new key as the next version of the same Key Vault secret and
                  never shows it again. It doesn&apos;t send the key to AWS. API Management picks it
                  up within four hours, so keep the old key working until then, and delete it in
                  AWS afterwards.
                </Text>
              ) : (
                <Text size={200} className={styles.muted}>
                  MOSAIC stores the new key as the next version of the same Key Vault secret,
                  checks it, and never shows it again. API Management picks it up within four
                  hours, so keep the old key working until then: paste the resource&apos;s other
                  key, and regenerate the old one afterwards.
                </Text>
              )}
              <Field
                label={bedrock ? 'New Bedrock API key' : 'New API key'}
                required
                validationMessage={
                  touched && !apiKey.trim()
                    ? bedrock
                      ? 'Paste the new Bedrock API key.'
                      : "Paste the resource's new API key."
                    : undefined
                }
              >
                <Input
                  type="password"
                  autoComplete="new-password"
                  spellCheck={false}
                  value={apiKey}
                  onChange={(_, data) => setApiKey(data.value)}
                />
              </Field>
            </DialogContent>
            <DialogActions>
              <Button appearance="secondary" disabledFocusable={replace.isPending} onClick={close}>
                Cancel
              </Button>
              <Button appearance="primary" type="submit" disabledFocusable={replace.isPending}>
                {replace.isPending ? 'Storing…' : 'Store new key'}
              </Button>
            </DialogActions>
          </DialogBody>
        </form>
      </DialogSurface>
    </Dialog>
  )
}

/**
 * The deployments declared on a key-authenticated endpoint, where they're declared and removed.
 * They take the place of the discovered models an endpoint registered by resource ID lists.
 */
export function DeclaredDeploymentsCard({
  endpoint,
  className,
}: {
  endpoint: ModelEndpoint
  className?: string
}) {
  const api = useMosaicApi()
  const queryClient = useQueryClient()
  const restoreFocus = useRestoreFocusTarget()
  const [declaring, setDeclaring] = useState(false)
  const [status, setStatus] = useState<string | null>(null)
  const declared = endpoint.declaredDeployments ?? []
  const bedrock = endpoint.provider === 'awsBedrock'

  const remove = useMutation({
    mutationFn: (deploymentName: string) =>
      api.removeDeclaredModelDeployment(endpoint.id, deploymentName),
    onSuccess: async (_, deploymentName) => {
      setStatus(
        `Removed ${deploymentName} from ${endpoint.name}. Nothing changed in ` +
          `${bedrock ? 'AWS' : 'Azure'}.`,
      )
      await queryClient.invalidateQueries({ queryKey: ['model-endpoints'] })
      await queryClient.invalidateQueries({ queryKey: ['publishable-models'] })
      await queryClient.invalidateQueries({ queryKey: ['publications'] })
      await queryClient.invalidateQueries({ queryKey: ['model-pools'] })
    },
    onError: () => setStatus(null),
  })
  // The removed row takes its button with it, so the outcome takes focus instead.
  const statusRef = useFocusOnChange(status, true)
  const errorRef = useFocusOnChange(remove.error, true)

  return (
    <Card className={className}>
      <div className={styles.panelHeader}>
        <div className={styles.panelText}>
          <Title3 as="h2">
            {`${bedrock ? 'Models on' : 'Deployments on'} ${endpoint.name}`}
          </Title3>
          {bedrock ? (
            <Text size={200} className={styles.muted}>
              You declared these: a Bedrock API key can&apos;t list models. MOSAIC serves them only
              as members of a model pool, through the Anthropic Messages API, and never checks them
              with AWS.
            </Text>
          ) : (
            <Text size={200} className={styles.muted}>
              You declared these: an API key can&apos;t list a resource&apos;s deployments. MOSAIC
              publishes each one through the API declared for it, and checks the key, not the
              deployment.
            </Text>
          )}
        </div>
        <Button
          appearance="secondary"
          icon={<AddRegular />}
          onClick={() => setDeclaring(true)}
          {...restoreFocus}
        >
          {bedrock ? 'Declare a model' : 'Declare a deployment'}
        </Button>
      </div>
      {status && (
        <div ref={statusRef} tabIndex={-1} role="status">
          <Text size={200}>{status}</Text>
        </div>
      )}
      {remove.isError && (
        <div ref={errorRef} tabIndex={-1}>
          <ErrorState
            title={
              bedrock ? "MOSAIC didn't remove this model" : "MOSAIC didn't remove this deployment"
            }
            error={remove.error}
          />
        </div>
      )}
      {declared.length === 0 ? (
        bedrock ? (
          <EmptyState title="No models declared">
            Declare the Bedrock model IDs you want to serve through a model pool.
          </EmptyState>
        ) : (
          <EmptyState title="No deployments declared">
            Declare the deployments on this resource that you want to publish.
          </EmptyState>
        )
      ) : (
        <Table aria-label={bedrock ? 'Declared Bedrock models' : 'Declared model deployments'}>
          <TableHeader>
            <TableRow>
              <TableHeaderCell>{bedrock ? 'Model ID' : 'Deployment'}</TableHeaderCell>
              <TableHeaderCell>Model</TableHeaderCell>
              <TableHeaderCell>API</TableHeaderCell>
              <TableHeaderCell>Source</TableHeaderCell>
              <TableHeaderCell>Actions</TableHeaderCell>
            </TableRow>
          </TableHeader>
          <TableBody>
            {declared.map((deployment) => {
              const removing = remove.isPending && remove.variables === deployment.deploymentName
              return (
                <TableRow key={deployment.deploymentName}>
                  <TableCell>{deployment.deploymentName}</TableCell>
                  <TableCell>
                    {deployment.modelName}
                    {deployment.modelVersion ? ` · ${deployment.modelVersion}` : ''}
                  </TableCell>
                  <TableCell>{API_SHAPE_LABELS[deployment.apiShape]}</TableCell>
                  <TableCell>Declared, not discovered</TableCell>
                  <TableCell>
                    <Button
                      appearance="subtle"
                      aria-label={`Remove ${deployment.deploymentName}`}
                      disabledFocusable={remove.isPending}
                      onClick={() => {
                        setStatus(null)
                        remove.mutate(deployment.deploymentName)
                      }}
                    >
                      {removing ? 'Removing…' : 'Remove'}
                    </Button>
                  </TableCell>
                </TableRow>
              )
            })}
          </TableBody>
        </Table>
      )}
      <DeclareDeploymentDialog
        endpoint={endpoint}
        open={declaring}
        onClose={() => setDeclaring(false)}
      />
    </Card>
  )
}
