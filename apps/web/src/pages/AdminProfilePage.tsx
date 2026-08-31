import { useMsal } from '@azure/msal-react'
import {
  Badge,
  Button,
  Card,
  Input,
  Text,
  Title3,
} from '@fluentui/react-components'
import { useMemo } from 'react'
import { PageHeader, PreviewNotice } from '../components/PageHeader'
import { runtimeConfig } from '../runtime-config'
import styles from './AdminProfilePage.module.css'

const sampleRoleAssignments = [
  {
    role: 'MOSAIC Platform Administrator',
    scope: 'Subscription / Shared AI Gateway',
    detail: 'Sample data for control-plane ownership and deployment approvals.',
  },
  {
    role: 'MOSAIC Support Reviewer',
    scope: 'Resource Group / Diagnostics',
    detail: 'Sample data for operational triage and support workflows.',
  },
  {
    role: 'MOSAIC Access Auditor',
    scope: 'Workspace / Identity reports',
    detail: 'Sample data for periodic RBAC review and governance checks.',
  },
]

const sampleProgrammaticAccess = [
  { label: 'Credential type', value: 'Sample federated workload identity' },
  { label: 'Client application', value: 'sample-mosaic-admin-app' },
  { label: 'Rotation policy', value: 'Sample quarterly review only' },
  { label: 'Secret material', value: 'Never displayed in MOSAIC' },
]

const sampleRecentActivity = [
  {
    title: 'Sample role review completed',
    timestamp: '2026-08-12 15:30 UTC',
    detail: 'Preview-only governance check for MOSAIC admin assignments.',
  },
  {
    title: 'Sample diagnostics export inspected',
    timestamp: '2026-08-11 09:10 UTC',
    detail: 'Preview-only review of support health signals and dashboard notes.',
  },
  {
    title: 'Sample token metadata reviewed',
    timestamp: '2026-08-09 18:45 UTC',
    detail: 'Preview-only access metadata validation. No secret was generated.',
  },
]

function initialsFor(name: string): string {
  return name
    .split(/\s+/)
    .map((part) => part[0] ?? '')
    .join('')
    .slice(0, 2)
    .toUpperCase()
}

export function AdminProfilePage() {
  const { instance, accounts } = useMsal()
  const account = accounts[0]
  const displayName = account?.name ?? 'Local administrator'
  const username = account?.username ?? 'No Entra account connected'
  const tenantId = account?.tenantId ?? runtimeConfig.entraTenantId
  const environment = account?.environment ?? 'local-preview'
  const homeAccountId = account?.homeAccountId ?? 'local-administrator'
  const initials = useMemo(() => initialsFor(displayName), [displayName])

  return (
    <section className={styles.page}>
      <PageHeader
        title="Admin Profile"
        description="Review administrator identity context and preview-only access metadata used in MOSAIC."
        source={runtimeConfig.authMode === 'entra' ? 'live' : 'local'}
        actions={
          runtimeConfig.authMode === 'entra' ? (
            <Button onClick={() => void instance.logoutRedirect()}>
              Sign out
            </Button>
          ) : undefined
        }
      />
      <PreviewNotice kind="sample">
        Role assignments, programmatic-access metadata, and recent activity below are sample data
        only. This page never generates or reveals credentials.
      </PreviewNotice>

      <div className={styles.grid}>
        <Card className={styles.card}>
          <div className={styles.identityHeader}>
            <div className={styles.avatar} aria-hidden="true">
              {initials}
            </div>
            <div>
              <Title3 as="h2">Administrator identity</Title3>
              <Text className={styles.cardDescription}>
                Account data comes from MSAL when available; otherwise MOSAIC uses an explicit local
                administrator placeholder.
              </Text>
            </div>
          </div>

          <dl className={styles.definitionList}>
            <div className={styles.definitionRow}>
              <dt>Display name</dt>
              <dd>{displayName}</dd>
            </div>
            <div className={styles.definitionRow}>
              <dt>Sign-in name</dt>
              <dd>{username}</dd>
            </div>
            <div className={styles.definitionRow}>
              <dt>Authentication mode</dt>
              <dd>{runtimeConfig.authMode}</dd>
            </div>
            <div className={styles.definitionRow}>
              <dt>Tenant ID</dt>
              <dd>{tenantId}</dd>
            </div>
            <div className={styles.definitionRow}>
              <dt>Environment</dt>
              <dd>{environment}</dd>
            </div>
            <div className={styles.definitionRow}>
              <dt>Home account ID</dt>
              <dd>{homeAccountId}</dd>
            </div>
          </dl>
        </Card>

        <Card className={styles.card}>
          <div className={styles.sectionHeader}>
            <div>
              <div className={styles.titleRow}>
                <Title3 as="h2">Role assignments</Title3>
                <Badge appearance="tint" className={styles.sampleBadge}>
                  Sample data
                </Badge>
              </div>
              <Text className={styles.cardDescription}>
                Example RBAC records for layout review only. They do not grant or revoke access.
              </Text>
            </div>
          </div>

          <div className={styles.list}>
            {sampleRoleAssignments.map((assignment) => (
              <div className={styles.listItem} key={assignment.role}>
                <div className={styles.itemHeader}>
                  <Text className={styles.emphasis}>{assignment.role}</Text>
                  <Text className={styles.scope}>{assignment.scope}</Text>
                </div>
                <Text className={styles.cardDescription}>{assignment.detail}</Text>
              </div>
            ))}
          </div>

          <div className={styles.actionRow}>
            <Button disabled>Request role change</Button>
            <Button disabled>Add assignment</Button>
          </div>
          <Text className={styles.helperText}>
            Disabled controls indicate preview-only behavior. Role changes must be handled outside
            this page, and the records above remain sample data.
          </Text>
        </Card>

        <Card className={styles.card}>
          <div className={styles.sectionHeader}>
            <div>
              <div className={styles.titleRow}>
                <Title3 as="h2">Programmatic access metadata</Title3>
                <Badge appearance="tint" className={styles.sampleBadge}>
                  Sample data
                </Badge>
              </div>
              <Text className={styles.cardDescription} id="credential-preview-help">
                Sample metadata only. Tokens, client secrets, certificates, and credential values
                are never generated or displayed here.
              </Text>
            </div>
          </div>

          <div className={styles.metadataGrid}>
            {sampleProgrammaticAccess.map((item) => (
              <div className={styles.metadataItem} key={item.label}>
                <Text className={styles.infoLabel}>{item.label}</Text>
                <Input aria-describedby="credential-preview-help" readOnly value={item.value} />
              </div>
            ))}
          </div>

          <div className={styles.actionRow}>
            <Button aria-describedby="credential-preview-help" disabled>
              Generate credential
            </Button>
            <Button aria-describedby="credential-preview-help" disabled>
              Rotate secret
            </Button>
          </div>
        </Card>

        <Card className={styles.card}>
          <div className={styles.sectionHeader}>
            <div>
              <div className={styles.titleRow}>
                <Title3 as="h2">Recent activity</Title3>
                <Badge appearance="tint" className={styles.sampleBadge}>
                  Sample data
                </Badge>
              </div>
              <Text className={styles.cardDescription}>
                Preview timeline for audit and support workflows.
              </Text>
            </div>
          </div>

          <div className={styles.timeline}>
            {sampleRecentActivity.map((activity) => (
              <div className={styles.timelineItem} key={activity.title}>
                <Text className={styles.emphasis}>{activity.title}</Text>
                <Text className={styles.infoLabel}>{activity.timestamp}</Text>
                <Text className={styles.cardDescription}>{activity.detail}</Text>
              </div>
            ))}
          </div>
        </Card>
      </div>
    </section>
  )
}
