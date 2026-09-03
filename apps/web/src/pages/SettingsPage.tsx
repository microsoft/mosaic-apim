import { useMsal } from '@azure/msal-react'
import {
  Button,
  Card,
  Field,
  Input,
  MessageBar,
  MessageBarBody,
  Radio,
  RadioGroup,
  Text,
  Title3,
} from '@fluentui/react-components'
import { type FormEvent, useState } from 'react'
import { PageHeader, PreviewNotice } from '../components/PageHeader'
import { runtimeConfig } from '../runtime-config'
import { type ThemePreference, useMosaicTheme } from '../theme-context'
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
        description="Review safe runtime details, personalize appearance, and preview local integration values without changing shared MOSAIC configuration."
        source="local"
      />
      <PreviewNotice kind="local">
        Integration values on this page are a browser-side preview only. MOSAIC keeps runtime
        configuration in deployed settings and never displays secrets here.
      </PreviewNotice>

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
