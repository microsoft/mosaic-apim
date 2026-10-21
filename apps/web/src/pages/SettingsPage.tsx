import { useMsal } from '@azure/msal-react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
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
import {
  GlobeRegular,
  MailRegular,
  SettingsRegular,
  WarningRegular,
  WeatherMoonRegular,
} from '@fluentui/react-icons'
import { useEffect, useMemo, useState } from 'react'
import { Navigate, useSearchParams } from 'react-router-dom'
import { ApiError, useMosaicApi } from '../api'
import { EnvironmentBadge } from '../components/EnvironmentBadge'
import { EnvironmentFindings } from '../components/EnvironmentFindings'
import { PublicationsBlockedRefusal } from '../components/EnvironmentRefusal'
import { ReviewEnvironmentSuggestionsDialog } from '../components/ReviewEnvironmentSuggestionsDialog'
import { PageHeader } from '../components/PageHeader'
import { SectionLayout, usePageSection } from '../components/SectionLayout'
import { ApprovalAiSettings } from '../components/ApprovalAiSettings'
import { NotificationSettings } from '../components/NotificationSettings'
import { DirectoryConfiguration } from '../components/DirectoryConfiguration'
import { AzureConnectionsPanel } from '../components/AzureConnectionsPanel'
import {
  environmentInUseDetails,
  invalidEnvironmentField,
  invalidateEnvironmentQueries,
  publicationsBlockedDetails,
  useEnvironmentCatalog,
} from '../environments'
import { plural } from '../labels'
import { runtimeConfig } from '../runtime-config'
import { type ThemePreference, useMosaicTheme } from '../theme-context'
import type {
  EnvironmentCatalogView,
  EnvironmentColor,
  EnvironmentCreate,
  EnvironmentUpdate,
} from '../types'
import styles from './SettingsPage.module.css'
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
          <Title3 as="h2">Environments</Title3>
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

export function SettingsPage() {
  const sections = [
    { value: 'environments', label: 'Environments', icon: <GlobeRegular />, description: 'Shared environment classifications and enforcement settings saved to MOSAIC.' },
    { value: 'connections', label: 'Azure connections', icon: <GlobeRegular />, description: 'Management identities for Azure Commercial and Azure Government resources.' },
    { value: 'findings', label: 'Findings', icon: <WarningRegular />, description: 'Findings about environment classification and compatibility.' },
    { value: 'approvalAi', label: 'AI approval assistance', icon: <SettingsRegular />, description: 'Configure a governed model for advisory review. Saved intent, connection verification, and response-contract readiness are distinct.' },
    { value: 'notifications', label: 'Notifications', icon: <MailRegular />, description: 'Configure shared channels, administrator routing, Teams destinations, and inspect delivery evidence.' },
    { value: 'runtime', label: 'Runtime configuration', icon: <SettingsRegular />, description: 'Read-only deployed configuration. Change deployment settings outside this page.' },
    { value: 'appearance', label: 'Appearance', icon: <WeatherMoonRegular />, description: 'Preferences apply immediately and are saved only in this browser.' },
  ]
  const [section, selectSection] = usePageSection(sections)
  const [params] = useSearchParams()
  const { accounts } = useMsal()
  const { preference, resolvedTheme, setPreference } = useMosaicTheme()

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

  if (params.get('tab') === 'email') {
    const next = new URLSearchParams(params)
    next.set('tab', 'notifications')
    next.set('notificationView', 'email')
    return <Navigate to={{ search: `?${next}` }} replace />
  }
  return (
    <section className={styles.page}>
      <PageHeader
        title="Settings"
        description="Manage Azure connections, classify environments, set up notifications, and personalize appearance."
      />
      <SectionLayout label="Settings" sections={sections} active={section} onSelect={selectSection}>
      {section === 'environments' && <EnvironmentsSettingsSection />}
      {section === 'connections' && <AzureConnectionsPanel />}
      {section === 'findings' && <EnvironmentFindings title="Findings" />}
      {section === 'approvalAi' && <ApprovalAiSettings />}
      {section === 'notifications' && <NotificationSettings />}

        {section === 'runtime' && <Card className={styles.card}>
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
          <DirectoryConfiguration />
        </Card>}

        {section === 'appearance' && <Card className={styles.card}>
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
        </Card>}

      </SectionLayout>
    </section>
  )
}
