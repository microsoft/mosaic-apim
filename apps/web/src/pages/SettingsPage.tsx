import { useMsal } from '@azure/msal-react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Badge,
  Button,
  Card,
  Checkbox,
  Dialog,
  DialogActions,
  DialogBody,
  DialogContent,
  DialogSurface,
  DialogTitle,
  Field,
  Input,
  Label,
  MessageBar,
  MessageBarActions,
  MessageBarBody,
  MessageBarTitle,
  Radio,
  RadioGroup,
  Spinner,
  Switch,
  Table,
  TableBody,
  TableCell,
  TableHeader,
  TableHeaderCell,
  TableRow,
  Text,
  Textarea,
  Title3,
  useId,
} from '@fluentui/react-components'
import { type FormEvent, useEffect, useMemo, useState } from 'react'
import { ApiError, useMosaicApi } from '../api'
import { EnvironmentBadge } from '../components/EnvironmentBadge'
import { EnvironmentFindings } from '../components/EnvironmentFindings'
import { PublicationsBlockedRefusal } from '../components/EnvironmentRefusal'
import { ReviewEnvironmentSuggestionsDialog } from '../components/ReviewEnvironmentSuggestionsDialog'
import { PageHeader, PreviewNotice, DataSourceBadge } from '../components/PageHeader'
import {
  environmentInUseDetails,
  invalidEnvironmentField,
  invalidateEnvironmentQueries,
  publicationsBlockedDetails,
  useEnvironmentCatalog,
} from '../environments'
import { plural } from '../labels'
import { formatTimestamp } from '../publication-state'
import { runtimeConfig } from '../runtime-config'
import { type ThemePreference, useMosaicTheme } from '../theme-context'
import type {
  EmailSettings,
  EnvironmentCatalogView,
  EnvironmentColor,
  EnvironmentCreate,
  EnvironmentUpdate,
} from '../types'
import styles from './SettingsPage.module.css'
interface LocalIntegrationSettings {
  supportAlias: string
  workspaceTag: string
  changeTemplate: string
}

const defaultIntegrations: LocalIntegrationSettings = {
  supportAlias: 'mosaic-admin-preview',
  workspaceTag: 'workspace-preview',
  changeTemplate: 'CHG-MOSAIC-LOCAL',
}

const appearanceOptions: Array<{
  value: ThemePreference
  label: string
  description: string
}> = [
  { value: 'light', label: 'Light', description: 'Always use the MOSAIC light theme.' },
  { value: 'dark', label: 'Dark', description: 'Always use the MOSAIC dark theme.' },
  {
    value: 'system',
    label: 'System',
    description: 'Follow your browser and operating system color-scheme preference.',
  },
]

const environmentColors: EnvironmentColor[] = [
  'brand',
  'danger',
  'important',
  'informative',
  'severe',
  'subtle',
  'success',
  'warning',
]
const environmentKeyPattern = /^[a-z][a-z0-9-]{1,31}$/

interface EnvironmentFormState {
  key: string
  displayName: string
  description: string
  color: EnvironmentColor
  production: boolean
  aliases: string
  acceptsEndpointsFrom: string[]
  order: string
}

function emptyEnvironmentForm(): EnvironmentFormState {
  return {
    key: '',
    displayName: '',
    description: '',
    color: 'brand',
    production: false,
    aliases: '',
    acceptsEndpointsFrom: [],
    order: '',
  }
}

function formFromEnvironment(catalog: EnvironmentCatalogView, key: string): EnvironmentFormState {
  const environment = catalog.environments.find((item) => item.key === key)
  if (!environment) return emptyEnvironmentForm()
  return {
    key: environment.key,
    displayName: environment.displayName,
    description: environment.description ?? '',
    color: environment.color,
    production: environment.production,
    aliases: environment.aliases.join(', '),
    acceptsEndpointsFrom: environment.acceptsEndpointsFrom,
    order: String(environment.order),
  }
}

function environmentPayload(form: EnvironmentFormState): EnvironmentCreate {
  return {
    key: form.key.trim(),
    displayName: form.displayName.trim(),
    description: form.description.trim() || null,
    color: form.color,
    production: form.production,
    aliases: form.aliases.split(',').map((alias) => alias.trim()).filter(Boolean),
    acceptsEndpointsFrom: form.acceptsEndpointsFrom,
    order: form.order ? Number(form.order) : undefined,
  }
}

function fieldError(error: unknown, field: string) {
  return invalidEnvironmentField(error) === field
    ? error instanceof ApiError
      ? error.message
      : 'Invalid value'
    : undefined
}

function EnvironmentEditDialog({
  catalog,
  editKey,
  open,
  onClose,
}: {
  catalog: EnvironmentCatalogView
  editKey: string | null
  open: boolean
  onClose: () => void
}) {
  const api = useMosaicApi()
  const queryClient = useQueryClient()
  const [form, setForm] = useState<EnvironmentFormState>(emptyEnvironmentForm())
  const [error, setError] = useState<unknown>(null)
  const exceptionsLabelId = useId('environment-exceptions-')
  const exceptionsHintId = useId('environment-exceptions-hint-')
  useEffect(() => {
    if (open) {
      setForm(editKey ? formFromEnvironment(catalog, editKey) : emptyEnvironmentForm())
      setError(null)
    }
  }, [open, editKey, catalog])
  const mutation = useMutation({
    mutationFn: async () => {
      if (!editKey) {
        const payload = environmentPayload(form)
        if (!environmentKeyPattern.test(payload.key))
          throw new ApiError(
            'Use 2–32 lowercase letters, numbers, or hyphens, starting with a letter.',
            422,
            { details: { reason: 'invalidEnvironment', field: 'key' } },
          )
        if (payload.key === 'unclassified')
          throw new ApiError('The key unclassified is reserved.', 422, {
            details: { reason: 'invalidEnvironment', field: 'key' },
          })
        return api.createEnvironment(payload)
      }
      const { key: _key, ...payload } = environmentPayload(form)
      return api.updateEnvironment(editKey, payload as EnvironmentUpdate)
    },
    onSuccess: (updated) => {
      queryClient.setQueryData(['environment-catalog'], updated)
      invalidateEnvironmentQueries(queryClient)
      onClose()
    },
    onError: (err) => setError(err),
  })
  const blocked = publicationsBlockedDetails(error)
  const nonProductionSelected =
    form.production &&
    form.acceptsEndpointsFrom.some(
      (key) => !catalog.environments.find((env) => env.key === key)?.production,
    )
  return (
    <Dialog
      open={open}
      onOpenChange={(_, data) => {
        if (!data.open && !mutation.isPending) onClose()
      }}
    >
      <DialogSurface>
        <DialogBody>
          <DialogTitle>{editKey ? 'Edit environment' : 'Add environment'}</DialogTitle>
          <DialogContent className={styles.envDialogContent}>
            {!editKey && (
              <Field label="Key" required validationMessage={fieldError(error, 'key')}>
                <Input
                  value={form.key}
                  onChange={(_, data) => setForm((current) => ({ ...current, key: data.value }))}
                />
              </Field>
            )}
            <Field
              label="Display name"
              required
              validationMessage={fieldError(error, 'displayName')}
            >
              <Input
                value={form.displayName}
                onChange={(_, data) =>
                  setForm((current) => ({ ...current, displayName: data.value }))
                }
              />
            </Field>
            <Field label="Description" validationMessage={fieldError(error, 'description')}>
              <Textarea
                value={form.description}
                onChange={(_, data) =>
                  setForm((current) => ({ ...current, description: data.value }))
                }
              />
            </Field>
            <Field label="Color">
              <RadioGroup
                className={styles.swatches}
                value={form.color}
                onChange={(_, data) =>
                  setForm((current) => ({ ...current, color: data.value as EnvironmentColor }))
                }
              >
                {environmentColors.map((color) => (
                  <Radio
                    key={color}
                    value={color}
                    label={
                      <span className={styles.swatchLabel}>
                        <span className={`${styles.swatch} ${styles[`swatch-${color}`]}`} />
                        {color}
                      </span>
                    }
                  />
                ))}
              </RadioGroup>
            </Field>
            <Switch
              checked={form.production}
              onChange={(_, data) =>
                setForm((current) => ({
                  ...current,
                  production: data.checked,
                  acceptsEndpointsFrom: data.checked
                    ? current.acceptsEndpointsFrom.filter(
                        (key) => catalog.environments.find((env) => env.key === key)?.production,
                      )
                    : current.acceptsEndpointsFrom,
                }))
              }
              label="Production-class environment"
            />
            <Field label="Aliases">
              <Input
                value={form.aliases}
                onChange={(_, data) => setForm((current) => ({ ...current, aliases: data.value }))}
                placeholder="prod, live"
              />
            </Field>
            <div
              role="group"
              aria-labelledby={exceptionsLabelId}
              aria-describedby={exceptionsHintId}
              className={styles.exceptionGroup}
            >
              <Label id={exceptionsLabelId}>Exceptions</Label>
              <Text id={exceptionsHintId} size={200} className={styles.exceptionHint}>
                Gateways in this environment may also front endpoints from the environments checked
                here.
                {form.production &&
                  ' A production-class environment may list only other production-class environments.'}
              </Text>
              <div className={styles.checkboxList}>
                {catalog.environments
                  .filter((env) => env.key !== editKey)
                  .map((env) => (
                    <Checkbox
                      key={env.key}
                      checked={form.acceptsEndpointsFrom.includes(env.key)}
                      disabled={form.production && !env.production}
                      label={env.displayName}
                      onChange={(_, data) =>
                        setForm((current) => ({
                          ...current,
                          acceptsEndpointsFrom: data.checked
                            ? [...current.acceptsEndpointsFrom, env.key]
                            : current.acceptsEndpointsFrom.filter((key) => key !== env.key),
                        }))
                      }
                    />
                  ))}
              </div>
            </div>
            {nonProductionSelected && (
              <MessageBar intent="warning">
                <MessageBarBody>
                  Remove non-production exceptions before saving this as production-class.
                </MessageBarBody>
              </MessageBar>
            )}
            <Field label="Order">
              <Input
                type="number"
                value={form.order}
                onChange={(_, data) => setForm((current) => ({ ...current, order: data.value }))}
              />
            </Field>
            {blocked && <PublicationsBlockedRefusal details={blocked} catalog={catalog} />}
            {error && !blocked && !invalidEnvironmentField(error) && (
              <MessageBar intent="error">
                <MessageBarBody>
                  {error instanceof Error ? error.message : 'The environment could not be saved.'}
                </MessageBarBody>
              </MessageBar>
            )}
          </DialogContent>
          <DialogActions>
            <Button disabled={mutation.isPending} onClick={onClose}>
              Cancel
            </Button>
            <Button
              appearance="primary"
              disabled={mutation.isPending || nonProductionSelected}
              onClick={() => mutation.mutate()}
            >
              {mutation.isPending ? 'Saving…' : 'Save'}
            </Button>
          </DialogActions>
        </DialogBody>
      </DialogSurface>
    </Dialog>
  )
}

function DeleteEnvironmentDialog({
  catalog,
  deleteKey,
  onClose,
}: {
  catalog: EnvironmentCatalogView
  deleteKey: string | null
  onClose: () => void
}) {
  const api = useMosaicApi()
  const queryClient = useQueryClient()
  const [error, setError] = useState<unknown>(null)
  const environment = catalog.environments.find((item) => item.key === deleteKey)
  const mutation = useMutation({
    mutationFn: () => api.deleteEnvironment(deleteKey ?? ''),
    onSuccess: (updated) => {
      queryClient.setQueryData(['environment-catalog'], updated)
      invalidateEnvironmentQueries(queryClient)
      onClose()
    },
    onError: setError,
  })
  const inUse = environmentInUseDetails(error)
  return (
    <Dialog
      open={deleteKey != null}
      onOpenChange={(_, data) => {
        if (!data.open && !mutation.isPending) onClose()
      }}
    >
      <DialogSurface>
        <DialogBody>
          <DialogTitle>Delete environment</DialogTitle>
          <DialogContent className={styles.envDialogContent}>
            <Text>
              Delete {environment?.displayName ?? deleteKey}? Resources cannot reference deleted
              environments.
            </Text>
            {inUse && (
              <MessageBar intent="error">
                <MessageBarBody>
                  <MessageBarTitle>Environment is in use</MessageBarTitle>
                  {plural(inUse.usage.gateways, 'gateway')},{' '}
                  {plural(inUse.usage.modelEndpoints, 'model endpoint')}, and{' '}
                  {plural(inUse.usage.mcpEndpoints, 'MCP server')} use it. Referenced by:{' '}
                  {inUse.referencedBy.join(', ') || 'None'}.
                </MessageBarBody>
              </MessageBar>
            )}
            {error instanceof ApiError && error.body?.details?.reason === 'builtInEnvironment' && (
              <MessageBar intent="error">
                <MessageBarBody>Built-in environments cannot be deleted.</MessageBarBody>
              </MessageBar>
            )}
            {error &&
              !inUse &&
              !(
                error instanceof ApiError && error.body?.details?.reason === 'builtInEnvironment'
              ) && (
                <MessageBar intent="error">
                  <MessageBarBody>
                    {error instanceof Error ? error.message : 'Delete failed.'}
                  </MessageBarBody>
                </MessageBar>
              )}
          </DialogContent>
          <DialogActions>
            <Button disabled={mutation.isPending} onClick={onClose}>
              Cancel
            </Button>
            <Button
              appearance="primary"
              disabled={mutation.isPending || environment?.builtIn}
              onClick={() => mutation.mutate()}
            >
              {mutation.isPending ? 'Deleting…' : 'Delete'}
            </Button>
          </DialogActions>
        </DialogBody>
      </DialogSurface>
    </Dialog>
  )
}

function EnvironmentsSettingsSection() {
  const api = useMosaicApi()
  const queryClient = useQueryClient()
  const catalog = useEnvironmentCatalog()
  const [editKey, setEditKey] = useState<string | null>(null)
  const [createOpen, setCreateOpen] = useState(false)
  const [deleteKey, setDeleteKey] = useState<string | null>(null)
  const [reviewOpen, setReviewOpen] = useState(false)
  const [settingsError, setSettingsError] = useState<unknown>(null)
  const settingsMutation = useMutation({
    mutationFn: (requireClassification: boolean) =>
      api.updateEnvironmentSettings({ requireClassification }),
    onSuccess: (updated) => {
      queryClient.setQueryData(['environment-catalog'], updated)
      invalidateEnvironmentQueries(queryClient)
      setSettingsError(null)
    },
    onError: setSettingsError,
  })
  const blocked = publicationsBlockedDetails(settingsError)
  const totalUnclassified = useMemo(
    () =>
      catalog.data
        ? catalog.data.unclassified.gateways +
          catalog.data.unclassified.modelEndpoints +
          catalog.data.unclassified.mcpEndpoints
        : 0,
    [catalog.data],
  )
  return (
    <Card className={`${styles.card} ${styles.wideCard}`}>
      <div className={styles.cardHeader}>
        <div>
          <div className={styles.liveHeading}>
            <Title3 as="h2">Environments</Title3>
            <DataSourceBadge kind="live" />
          </div>
          <Text className={styles.cardDescription}>
            Classify gateways, model endpoints, and MCP servers before environment rules are
            enforced.
          </Text>
        </div>
        <Button appearance="primary" onClick={() => setCreateOpen(true)}>
          Add environment
        </Button>
      </div>
      {catalog.isLoading && <Spinner label="Loading environments" />}
      {catalog.error && (
        <MessageBar intent="error">
          <MessageBarBody>
            {catalog.error instanceof Error
              ? catalog.error.message
              : 'Environments could not be loaded.'}
          </MessageBarBody>
        </MessageBar>
      )}
      {catalog.data && (
        <>
          <div className={styles.tableWrap}>
            <Table aria-label="Environments">
              <TableHeader>
                <TableRow>
                  <TableHeaderCell>Environment</TableHeaderCell>
                  <TableHeaderCell>Key</TableHeaderCell>
                  <TableHeaderCell>Production-class</TableHeaderCell>
                  <TableHeaderCell>Exceptions</TableHeaderCell>
                  <TableHeaderCell className={styles.usageColumn}>Usage</TableHeaderCell>
                  <TableHeaderCell>Built-in</TableHeaderCell>
                  <TableHeaderCell>Actions</TableHeaderCell>
                </TableRow>
              </TableHeader>
              <TableBody>
                {catalog.data.environments.map((environment) => (
                  <TableRow key={environment.key}>
                    <TableCell>
                      <EnvironmentBadge environment={environment.key} catalog={catalog.data} />
                    </TableCell>
                    <TableCell>{environment.key}</TableCell>
                    <TableCell>{environment.production ? 'Yes' : 'No'}</TableCell>
                    <TableCell>
                      {environment.acceptsEndpointsFrom.length === 0
                        ? 'None'
                        : environment.acceptsEndpointsFrom.map((key) => (
                            <EnvironmentBadge key={key} environment={key} catalog={catalog.data} />
                          ))}
                    </TableCell>
                    <TableCell>
                      <div className={styles.usageCounts}>
                        <span>{plural(environment.usage.gateways, 'gateway')}</span>
                        <span>{plural(environment.usage.modelEndpoints, 'model endpoint')}</span>
                        <span>{plural(environment.usage.mcpEndpoints, 'MCP server')}</span>
                      </div>
                    </TableCell>
                    <TableCell>
                      {environment.builtIn ? (
                        <Badge appearance="outline">Built-in</Badge>
                      ) : (
                        'Custom'
                      )}
                    </TableCell>
                    <TableCell>
                      <Button size="small" onClick={() => setEditKey(environment.key)}>
                        Edit
                      </Button>
                      {!environment.builtIn && (
                        <Button size="small" onClick={() => setDeleteKey(environment.key)}>
                          Delete
                        </Button>
                      )}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
          <div className={styles.environmentControls}>
            <Switch
              checked={catalog.data.requireClassification}
              disabled={settingsMutation.isPending}
              onChange={(_, data) => settingsMutation.mutate(data.checked)}
              label="Require classification before publishing"
            />
            <Card className={styles.unclassifiedCard}>
              <Title3 as="h3">Unclassified</Title3>
              <Text>
                {plural(totalUnclassified, 'resource')}{' '}
                {totalUnclassified === 1 ? 'needs' : 'need'} classification:{' '}
                {plural(catalog.data.unclassified.gateways, 'gateway')},{' '}
                {plural(catalog.data.unclassified.modelEndpoints, 'model endpoint')},{' '}
                {plural(catalog.data.unclassified.mcpEndpoints, 'MCP server')}.
              </Text>
              <Button className={styles.reviewButton} onClick={() => setReviewOpen(true)}>
                Review suggestions
              </Button>
            </Card>
          </div>
          {blocked && <PublicationsBlockedRefusal details={blocked} catalog={catalog.data} />}
          {settingsError && !blocked && (
            <MessageBar intent="error">
              <MessageBarBody>
                {settingsError instanceof Error
                  ? settingsError.message
                  : 'Settings could not be updated.'}
              </MessageBarBody>
            </MessageBar>
          )}
          <EnvironmentEditDialog
            catalog={catalog.data}
            open={createOpen || editKey != null}
            editKey={editKey}
            onClose={() => {
              setCreateOpen(false)
              setEditKey(null)
            }}
          />
          <DeleteEnvironmentDialog
            catalog={catalog.data}
            deleteKey={deleteKey}
            onClose={() => setDeleteKey(null)}
          />
          <ReviewEnvironmentSuggestionsDialog
            open={reviewOpen}
            onClose={() => setReviewOpen(false)}
          />
        </>
      )}
    </Card>
  )
}

interface EmailFormState {
  enabled: boolean
  endpoint: string
  sender: string
}

function emailForm(settings: EmailSettings): EmailFormState {
  return {
    enabled: settings.enabled,
    endpoint: settings.endpoint ?? '',
    sender: settings.sender ?? '',
  }
}

function emailStatus(settings: EmailSettings): {
  label: string
  color: 'success' | 'informative' | 'warning'
} {
  if (settings.ready) return { label: 'On', color: 'success' }
  if (settings.endpoint && settings.sender) return { label: 'Off', color: 'informative' }
  return { label: 'Not set up', color: 'warning' }
}

function lastTestText(settings: EmailSettings): string {
  if (!settings.lastTestAt) return 'No test email has been sent.'
  const when = formatTimestamp(settings.lastTestAt)
  return settings.lastTestError
    ? `The last test, ${when}, failed: ${settings.lastTestError}`
    : `The last test, ${when}, was accepted by Communication Services.`
}

function errorText(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback
}

interface Outcome {
  intent: 'success' | 'error'
  text: string
}

function EmailSettingsSection() {
  const api = useMosaicApi()
  const queryClient = useQueryClient()
  const settings = useQuery({ queryKey: ['email-settings'], queryFn: () => api.getEmailSettings() })
  const [form, setForm] = useState<EmailFormState>({ enabled: false, endpoint: '', sender: '' })
  const [syncedWith, setSyncedWith] = useState<string | null>(null)
  const [saveOutcome, setSaveOutcome] = useState<Outcome | null>(null)
  const [testTo, setTestTo] = useState('')
  const [testOutcome, setTestOutcome] = useState<Outcome | null>(null)
  const switchHintId = useId('email-switch-hint-')

  // Reset the form whenever the saved configuration changes, but not when only the last test does.
  const saved = settings.data ? emailForm(settings.data) : null
  const savedKey = saved ? `${saved.enabled}|${saved.endpoint}|${saved.sender}` : null
  if (saved && savedKey !== syncedWith) {
    setSyncedWith(savedKey)
    setForm(saved)
  }

  const save = useMutation({
    mutationFn: () =>
      api.saveEmailSettings({
        enabled: form.enabled,
        endpoint: form.endpoint.trim() || null,
        sender: form.sender.trim() || null,
      }),
    onSuccess: (updated) => {
      queryClient.setQueryData(['email-settings'], updated)
      void queryClient.invalidateQueries({ queryKey: ['budgets'] })
      setSaveOutcome({
        intent: 'success',
        text: updated.ready
          ? 'Saved. Budget email goes out through this Communication Services resource.'
          : 'Saved. Budget email stays off until you turn it on.',
      })
    },
    onError: (error) =>
      setSaveOutcome({ intent: 'error', text: errorText(error, 'The email settings could not be saved.') }),
  })
  const test = useMutation({
    mutationFn: (to: string) => api.sendTestEmail(to),
    onSuccess: (result) => {
      void queryClient.invalidateQueries({ queryKey: ['email-settings'] })
      setTestOutcome(
        result.sent
          ? {
              intent: 'success',
              text: `Communication Services accepted a test email to ${result.to}. If it doesn't arrive in a few minutes, check the junk folder.`,
            }
          : {
              intent: 'error',
              text: `Communication Services didn't accept the test email: ${result.error ?? 'no reason was given'}.`,
            },
      )
    },
    onError: (error) =>
      setTestOutcome({ intent: 'error', text: errorText(error, 'The test email could not be sent.') }),
  })

  const dirty =
    saved != null &&
    (form.enabled !== saved.enabled ||
      form.endpoint.trim() !== saved.endpoint ||
      form.sender.trim() !== saved.sender)
  const data = settings.data
  const suggestion =
    data?.suggestedEndpoint &&
    data.suggestedSender &&
    (data.suggestedEndpoint !== form.endpoint.trim() || data.suggestedSender !== form.sender.trim())
      ? { endpoint: data.suggestedEndpoint, sender: data.suggestedSender }
      : null
  const configured = Boolean(saved?.endpoint && saved.sender)

  function update(change: Partial<EmailFormState>) {
    setForm((current) => ({ ...current, ...change }))
    setSaveOutcome(null)
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (form.enabled && !(form.endpoint.trim() && form.sender.trim())) {
      setSaveOutcome({
        intent: 'error',
        text: 'Email can be on only with a Communication Services endpoint and a sender address.',
      })
      return
    }
    save.mutate()
  }

  function sendTest(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const to = testTo.trim()
    if (!to) {
      setTestOutcome({ intent: 'error', text: 'Enter the address to send the test email to.' })
      return
    }
    setTestOutcome(null)
    test.mutate(to)
  }

  const status = data ? emailStatus(data) : null
  return (
    <Card className={`${styles.card} ${styles.wideCard}`}>
      <div className={styles.cardHeader}>
        <div>
          <div className={styles.liveHeading}>
            <Title3 as="h2">Email</Title3>
            <DataSourceBadge kind="live" />
          </div>
          <Text className={styles.cardDescription}>
            Budget warnings and blocks are emailed through Azure Communication Services. MOSAIC
            signs in to it as its own managed identity, so no key or connection string is kept.
          </Text>
        </div>
        {status && (
          <Badge appearance="tint" color={status.color} size="large">
            {status.label}
          </Badge>
        )}
      </div>
      {settings.isLoading && <Spinner label="Loading email settings" />}
      {settings.error && (
        <MessageBar intent="error">
          <MessageBarBody>{errorText(settings.error, 'Email settings could not be loaded.')}</MessageBarBody>
        </MessageBar>
      )}
      {data && (
        <div className={styles.emailLayout}>
          <form className={styles.integrationForm} onSubmit={submit} noValidate aria-label="Email settings">
            <div className={styles.emailSwitch}>
              <Switch
                checked={form.enabled}
                onChange={(_, change) => update({ enabled: change.checked })}
                label="Send budget email"
                aria-describedby={switchHintId}
              />
              <Text id={switchHintId} size={200} className={styles.caption}>
                While it’s off, budgets still show on the Dashboard and still block calls, but
                nobody is emailed.
              </Text>
            </div>
            <Field
              label="Communication Services endpoint"
              hint="Like https://contoso-mosaic.communication.azure.com"
            >
              <Input
                type="url"
                value={form.endpoint}
                onChange={(_, change) => update({ endpoint: change.value })}
              />
            </Field>
            <Field
              label="Sender address"
              hint="An address on a domain connected to that resource, like DoNotReply@contoso.com"
            >
              <Input
                type="email"
                value={form.sender}
                onChange={(_, change) => update({ sender: change.value })}
              />
            </Field>
            {suggestion && (
              <MessageBar intent="info">
                <MessageBarBody>
                  <MessageBarTitle>This deployment has Communication Services</MessageBarTitle>
                  It created an email resource and a sender address for MOSAIC.
                </MessageBarBody>
                <MessageBarActions>
                  <Button size="small" onClick={() => update(suggestion)}>
                    Use them
                  </Button>
                </MessageBarActions>
              </MessageBar>
            )}
            <div className={styles.formActions}>
              <Button appearance="primary" type="submit" disabled={save.isPending || !dirty}>
                {save.isPending ? 'Saving…' : 'Save email settings'}
              </Button>
            </div>
            {saveOutcome && (
              <MessageBar intent={saveOutcome.intent}>
                <MessageBarBody>{saveOutcome.text}</MessageBarBody>
              </MessageBar>
            )}
          </form>
          <form className={styles.emailTest} onSubmit={sendTest} noValidate aria-label="Test email">
            <Title3 as="h3">Send a test</Title3>
            <Text className={styles.caption}>
              {configured
                ? 'Sends from the saved settings, even while email is off, to check the endpoint, the sender, and MOSAIC’s role on the resource.'
                : 'Save an endpoint and a sender address first.'}
            </Text>
            <Field label="Send to">
              <Input
                type="email"
                value={testTo}
                onChange={(_, change) => {
                  setTestTo(change.value)
                  setTestOutcome(null)
                }}
              />
            </Field>
            <Button
              type="submit"
              className={styles.emailTestButton}
              disabled={!configured || dirty || test.isPending}
            >
              {test.isPending ? 'Sending…' : 'Send test email'}
            </Button>
            {dirty && configured && (
              <Text size={200} className={styles.caption}>
                Save your changes first. The test uses the saved settings.
              </Text>
            )}
            {testOutcome && (
              <MessageBar intent={testOutcome.intent}>
                <MessageBarBody>{testOutcome.text}</MessageBarBody>
              </MessageBar>
            )}
            <Text size={200} className={styles.caption}>
              {lastTestText(data)}
            </Text>
          </form>
        </div>
      )}
      <Text className={styles.caption}>
        MOSAIC’s managed identity needs the Communication and Email Service Owner role, or a
        narrower custom role that can send email, on the Communication Services resource.
      </Text>
    </Card>
  )
}

export function SettingsPage() {
  const { accounts } = useMsal()
  const { preference, resolvedTheme, setPreference } = useMosaicTheme()
  const [integrations, setIntegrations] =
    useState<LocalIntegrationSettings>(defaultIntegrations)
  const [saveMessage, setSaveMessage] = useState<string | null>(null)

  const administratorLabel = accounts[0]?.name ?? 'Local administrator'
  const safeRuntimeValues = [
    { label: 'Administrator context', value: administratorLabel },
    { label: 'Authentication mode', value: runtimeConfig.authMode },
    { label: 'Tenant ID', value: runtimeConfig.entraTenantId },
    { label: 'API base URL', value: runtimeConfig.apiBaseUrl },
    {
      label: 'Application Insights',
      value: runtimeConfig.applicationInsightsConnectionString ? 'Configured' : 'Not configured',
    },
  ]

  function updateIntegrationField(
    field: keyof LocalIntegrationSettings,
    value: string,
  ) {
    setIntegrations((current) => ({ ...current, [field]: value }))
    setSaveMessage(null)
  }

  function handleSavePreview(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setSaveMessage(
      'Local preview saved in this page state only. Shared MOSAIC runtime configuration was not changed.',
    )
  }

  function handleResetPreview() {
    setIntegrations(defaultIntegrations)
    setSaveMessage(null)
  }

  return (
    <section className={styles.page}>
      <PageHeader
        title="Settings"
        description="Classify environments, set up budget email, review safe runtime details, and personalize appearance."
      />
      <PreviewNotice kind="local">
        Environments and Email are saved to MOSAIC. Integration overview is a browser-side preview
        only. MOSAIC keeps the rest of its configuration in deployed settings and never displays
        secrets here.
      </PreviewNotice>

      <EnvironmentsSettingsSection />
      <EnvironmentFindings title="Findings" />
      <EmailSettingsSection />

      <div className={styles.grid}>
        <Card className={styles.card}>
          <div className={styles.cardHeader}>
            <div>
              <Title3 as="h2">Runtime identity and configuration</Title3>
              <Text className={styles.cardDescription}>
                Only safe runtime values are shown for verification and troubleshooting.
              </Text>
            </div>
          </div>
          <dl className={styles.definitionList}>
            {safeRuntimeValues.map((item) => (
              <div key={item.label} className={styles.definitionRow}>
                <dt>{item.label}</dt>
                <dd>{item.value}</dd>
              </div>
            ))}
          </dl>
          <Text className={styles.caption}>
            Secrets, tokens, client secrets, and connection-string contents are never shown or
            stored in MOSAIC settings.
          </Text>
        </Card>

        <Card className={styles.card}>
          <div className={styles.cardHeader}>
            <div>
              <Title3 as="h2">Appearance</Title3>
              <Text className={styles.cardDescription}>
                Theme changes apply immediately and stay in this browser through local storage.
              </Text>
            </div>
          </div>
          <RadioGroup
            aria-label="MOSAIC theme preference"
            className={styles.radioGroup}
            value={preference}
            onChange={(_, data) => setPreference(data.value as ThemePreference)}
          >
            {appearanceOptions.map((option) => (
              <div key={option.value} className={styles.radioOption}>
                <Radio label={option.label} value={option.value} />
                <Text className={styles.radioDescription}>{option.description}</Text>
              </div>
            ))}
          </RadioGroup>
          <div className={styles.appearanceSummary}>
            <Text className={styles.emphasis}>Active theme</Text>
            <Text>
              Preference: {preference}. Resolved theme: {resolvedTheme}.
            </Text>
            <Text className={styles.caption}>
              The preference is saved per browser profile, so System follows your device setting
              while Light and Dark override it immediately.
            </Text>
          </div>
        </Card>

        <Card className={styles.card}>
          <div className={styles.cardHeader}>
            <div>
              <Title3 as="h2">Integration overview</Title3>
              <Text className={styles.cardDescription}>
                These fields are editable for local preview only and do not update deployed
                integrations.
              </Text>
            </div>
          </div>
          <form className={styles.integrationForm} onSubmit={handleSavePreview}>
            <Field label="Support destination alias">
              <Input
                value={integrations.supportAlias}
                onChange={(_, data) => updateIntegrationField('supportAlias', data.value)}
              />
            </Field>
            <Field label="Observability workspace tag">
              <Input
                value={integrations.workspaceTag}
                onChange={(_, data) => updateIntegrationField('workspaceTag', data.value)}
              />
            </Field>
            <Field label="Change template ID">
              <Input
                value={integrations.changeTemplate}
                onChange={(_, data) => updateIntegrationField('changeTemplate', data.value)}
              />
            </Field>
            <div className={styles.formActions}>
              <Button appearance="primary" type="submit">
                Save local preview
              </Button>
              <Button type="button" onClick={handleResetPreview}>
                Reset preview
              </Button>
            </div>
          </form>
          {saveMessage && (
            <MessageBar intent="success">
              <MessageBarBody>{saveMessage}</MessageBarBody>
            </MessageBar>
          )}
        </Card>
      </div>
    </section>
  )
}
