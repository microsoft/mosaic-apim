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
import { useMosaicApi } from '../api'
import { EmptyState, ErrorState } from './AsyncState'
import {
  API_SHAPE_LABELS,
  API_SHAPE_SHORT_LABELS,
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
 * a resource's deployments, so the administrator names the ones to publish.
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

  function update(key: number, change: Partial<DeclarationDraft>) {
    onChange(
      drafts.map((draft) => {
        if (draft.key !== key) return draft
        const next = { ...draft, ...change }
        if (change.modelName !== undefined && !next.shapeChosen) {
          next.apiShape = suggestedShape(next.modelName, provider)
        }
        return next
      }),
    )
  }

  return (
    <fieldset className={styles.declarations}>
      <legend>Deployments to publish</legend>
      <Text size={200} className={styles.muted}>
        An API key can&apos;t list a resource&apos;s deployments, so name the ones to publish and
        the API each one takes. You can declare more later.
      </Text>
      {drafts.length > 0 && (
        <div className={styles.declarationHeader} aria-hidden="true">
          <span>Deployment name</span>
          <span>Model</span>
          <span>API</span>
          <span>Remove</span>
        </div>
      )}
      {drafts.map((draft, index) => (
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
            onClick={() => onChange(drafts.filter((item) => item.key !== draft.key))}
          />
        </div>
      ))}
      <div>
        <Button
          appearance="secondary"
          icon={<AddRegular />}
          onClick={() => onChange([...drafts, blankDeclaration(provider)])}
        >
          Add a deployment
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
            <DialogTitle>Declare a deployment on {endpoint.name}</DialogTitle>
            <DialogContent className={styles.form}>
              {declare.isError && (
                <div ref={errorRef} tabIndex={-1}>
                  <ErrorState title="MOSAIC didn't declare this deployment" error={declare.error} />
                </div>
              )}
              <Text size={200} className={styles.muted}>
                Use the deployment&apos;s name exactly as the resource shows it. MOSAIC can&apos;t
                confirm it exists without calling the model, so a wrong name shows up as a 404 when
                the published API is called.
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
                <Select value={shape} onChange={(event) => setApiShape(event.target.value as ApiShape)}>
                  {shapes.map((item) => (
                    <option key={item} value={item}>
                      {API_SHAPE_LABELS[item]}
                    </option>
                  ))}
                </Select>
              </Field>
            </DialogContent>
            <DialogActions>
              <Button appearance="secondary" disabledFocusable={declare.isPending} onClick={close}>
                Cancel
              </Button>
              <Button appearance="primary" type="submit" disabledFocusable={declare.isPending}>
                {declare.isPending ? 'Declaring…' : 'Declare deployment'}
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

  const remove = useMutation({
    mutationFn: (deploymentName: string) =>
      api.removeDeclaredModelDeployment(endpoint.id, deploymentName),
    onSuccess: async (_, deploymentName) => {
      setStatus(`Removed ${deploymentName} from ${endpoint.name}. Nothing changed in Azure.`)
      await queryClient.invalidateQueries({ queryKey: ['model-endpoints'] })
      await queryClient.invalidateQueries({ queryKey: ['publishable-models'] })
      await queryClient.invalidateQueries({ queryKey: ['publications'] })
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
          <Title3 as="h2">Deployments on {endpoint.name}</Title3>
          <Text size={200} className={styles.muted}>
            You declared these: an API key can&apos;t list a resource&apos;s deployments. MOSAIC
            publishes each one through the API declared for it, and checks the key, not the
            deployment.
          </Text>
        </div>
        <Button
          appearance="secondary"
          icon={<AddRegular />}
          onClick={() => setDeclaring(true)}
          {...restoreFocus}
        >
          Declare a deployment
        </Button>
      </div>
      {status && (
        <div ref={statusRef} tabIndex={-1} role="status">
          <Text size={200}>{status}</Text>
        </div>
      )}
      {remove.isError && (
        <div ref={errorRef} tabIndex={-1}>
          <ErrorState title="MOSAIC didn't remove this deployment" error={remove.error} />
        </div>
      )}
      {declared.length === 0 ? (
        <EmptyState title="No deployments declared">
          Declare the deployments on this resource that you want to publish.
        </EmptyState>
      ) : (
        <Table aria-label="Declared model deployments">
          <TableHeader>
            <TableRow>
              <TableHeaderCell>Deployment</TableHeaderCell>
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
