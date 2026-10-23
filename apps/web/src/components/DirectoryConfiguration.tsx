import {
  Badge,
  Button,
  Link,
  MessageBar,
  MessageBarBody,
  MessageBarTitle,
  Text,
  Title3,
} from '@fluentui/react-components'
import { useQuery } from '@tanstack/react-query'
import { useMosaicApi } from '../api'
import { runtimeConfig } from '../runtime-config'
import type { DirectoryEntraApplication, DirectoryPermissionStatus } from '../types'
import { ErrorState, Loading } from './AsyncState'
import { DirectoryGroupVerification } from './DirectoryGroupVerification'

const GRAPH_APP_ID = '00000003-0000-0000-c000-000000000000'

const GRAPH_APP_ROLE_IDS: Record<string, string> = {
  'User.ReadBasic.All': '97235f07-e226-4f63-ace3-39588e11d3a1',
  'GroupMember.Read.All': '98830695-27a2-44f7-8c18-0c3ebc9698f6',
  'AgentIdentity.Read.All': 'b2b8f011-2898-4234-9092-5059f6c1ebfa',
  'Application.Read.All': '9a5d68dd-52b0-4cc2-bd40-abcf44ac3a30',
  'ProfilePhoto.Read.All': 'e24d31aa-e1ab-4c80-85fe-23018690335d',
}

function inferApiSiteName(apiBaseUrl: string): string | null {
  try {
    const host = new URL(apiBaseUrl).hostname.toLowerCase()
    if (host.endsWith('.azurewebsites.net')) {
      return host.slice(0, -'.azurewebsites.net'.length) || null
    }
  } catch {
    // Ignore non-URL base paths
  }
  return null
}

function inferEnvLabel(siteName: string | null): string | null {
  if (!siteName) return null
  const match = /^mosaic-(.+)-api-[a-z0-9]{6}$/i.exec(siteName)
  return match ? match[1] : null
}

function resolveEntraApplications(
  supplied: DirectoryEntraApplication[] | undefined,
  envLabel: string | null,
): DirectoryEntraApplication[] {
  const apps = [...(supplied ?? [])]
  if (runtimeConfig.authMode !== 'entra') {
    return apps
  }
  if (!apps.some((app) => app.role === 'api')) {
    const scopeMatch = /^api:\/\/([0-9a-f-]{36})\//i.exec(runtimeConfig.entraApiScope)
    if (scopeMatch) {
      apps.push({
        role: 'api',
        label: 'Control-plane API app registration',
        displayName: envLabel ? `mosaic-${envLabel}-api` : null,
        clientId: scopeMatch[1],
        groupClaimsTarget: true,
      })
    }
  }
  if (
    !apps.some((app) => app.role === 'spa') &&
    runtimeConfig.entraClientId &&
    runtimeConfig.entraClientId !== 'local-development'
  ) {
    apps.push({
      role: 'spa',
      label: 'Administrator console SPA app registration',
      displayName: envLabel ? `mosaic-${envLabel}-spa` : null,
      clientId: runtimeConfig.entraClientId,
      groupClaimsTarget: false,
    })
  }
  return apps
}

function permissionRoleId(permission: DirectoryPermissionStatus): string | null {
  return permission.appRoleId ?? GRAPH_APP_ROLE_IDS[permission.permission] ?? null
}

function buildCliCommands(
  principalId: string | null,
  siteName: string | null,
  graphAppId: string,
  requiredPermissions: DirectoryPermissionStatus[],
  optionalPermissions: DirectoryPermissionStatus[],
): string {
  const lines: string[] = []
  if (principalId) {
    lines.push(`PRINCIPAL_ID="${principalId}"`)
  } else if (siteName) {
    lines.push(
      `PRINCIPAL_ID=$(az ad sp list --filter "displayName eq '${siteName}'" --query "[0].id" -o tsv)`,
    )
  } else {
    lines.push('PRINCIPAL_ID="<api-managed-identity-object-id>"')
  }
  lines.push(`GRAPH_SP_ID=$(az ad sp show --id ${graphAppId} --query id -o tsv)`)
  const appendAssignment = (permission: DirectoryPermissionStatus) => {
    const roleId = permissionRoleId(permission) ?? '<app-role-id>'
    lines.push(
      `# ${permission.permission} (${permission.required ? 'Required' : 'Optional'}: ${permission.capability})`,
      'az rest --method POST \\',
      '  --url "https://graph.microsoft.com/v1.0/servicePrincipals/$PRINCIPAL_ID/appRoleAssignments" \\',
      '  --headers Content-Type=application/json \\',
      `  --body "{\\"principalId\\":\\"$PRINCIPAL_ID\\",\\"resourceId\\":\\"$GRAPH_SP_ID\\",\\"appRoleId\\":\\"${roleId}\\"}"`,
    )
  }
  for (const permission of requiredPermissions) {
    appendAssignment(permission)
  }
  for (const permission of optionalPermissions) {
    appendAssignment(permission)
  }
  return lines.join('\n')
}

export function DirectoryConfiguration() {
  const api = useMosaicApi()
  const status = useQuery({
    queryKey: ['directory', 'status', 'permissions'],
    queryFn: () => api.getDirectoryStatus(true),
  })
  const missingRequired = status.data?.permissions.filter(
    (permission) => permission.required && permission.granted === false,
  ) ?? []
  const missingOptional = status.data?.permissions.filter(
    (permission) => !permission.required && permission.granted === false,
  ) ?? []
  const permissionsChecked = status.data?.permissions.some(
    (permission) => permission.granted !== null && permission.granted !== undefined,
  ) ?? false
  const apiIdentityName =
    status.data?.apiIdentity?.displayName ?? inferApiSiteName(runtimeConfig.apiBaseUrl)
  const apiIdentityObjectId = status.data?.apiIdentity?.objectId ?? null
  const apiIdentityAppId = status.data?.apiIdentity?.appId ?? null
  const graphAppId = status.data?.graphAppId ?? GRAPH_APP_ID
  const envLabel = inferEnvLabel(apiIdentityName)
  const entraApplications = resolveEntraApplications(status.data?.entraApplications, envLabel)
  const apiApp = entraApplications.find((app) => app.role === 'api')
  const modelRuntimeApp = entraApplications.find((app) => app.role === 'modelRuntime')
  const groupClaimsTargets = entraApplications.filter((app) => app.groupClaimsTarget)

  return (
    <section className="directory-configuration">
      <Title3 as="h3">Microsoft Entra directory</Title3>
      {status.isPending ? <Loading label="Checking directory configuration" /> : status.isError ? (
        <ErrorState title="Directory configuration unavailable" error={status.error} />
      ) : (
        <MessageBar intent={status.data.lookupEnabled ? 'info' : 'warning'}>
          <MessageBarBody>
            {status.data.lookupEnabled
              ? "Directory lookup is enabled with Entra authentication. The permission status below comes from the API identity's current Microsoft Graph token."
              : 'Directory lookup requires Entra authentication and is unavailable with local authentication. Use manual entry to record identities without directory verification.'}
          </MessageBarBody>
        </MessageBar>
      )}
      {status.data?.lookupEnabled && (
        <MessageBar
          intent={
            !permissionsChecked || missingRequired.length > 0
              ? 'warning'
              : 'success'
          }
        >
          <MessageBarBody>
            <MessageBarTitle>
              {!permissionsChecked
                ? 'Microsoft Graph consent could not be verified'
                : missingRequired.length > 0
                  ? `${missingRequired.length} required Graph permission${missingRequired.length === 1 ? ' is' : 's are'} missing`
                  : 'Required Microsoft Graph permissions are granted'}
            </MessageBarTitle>
            <Text block>
              {!permissionsChecked
                ? `MOSAIC could not read the application permissions in its Graph token${status.data.permissionStatusMessage ? `: ${status.data.permissionStatusMessage}` : '.'}`
                : missingRequired.length > 0
                  ? 'Directory searches for the affected identity types will fail until an administrator grants consent.'
                  : missingOptional.length > 0
                    ? `${missingOptional.length} optional permission${missingOptional.length === 1 ? ' is' : 's are'} missing. Core people, group, and agent search remains available.`
                    : 'People, group, agent, application, and photo lookup are consented.'}
            </Text>
          </MessageBarBody>
        </MessageBar>
      )}
      {status.data?.lookupEnabled && !status.data.groupClaimsEnabled && (
        <MessageBar intent="warning">
          <MessageBarBody>
            <MessageBarTitle>Entra security-group token claims are not enabled</MessageBarTitle>
            <Text block>
              MOSAIC can record security-group grants, but gateways cannot enforce them until{' '}
              <code>groupMembershipClaims</code> is set to <code>SecurityGroup</code> on the{' '}
              {modelRuntimeApp ? (
                <>
                  model-runtime app registration (
                  {modelRuntimeApp.displayName && (
                    <>
                      <code>{modelRuntimeApp.displayName}</code>,{' '}
                    </>
                  )}
                  client ID <code>{modelRuntimeApp.clientId}</code>)
                </>
              ) : (
                'model-runtime app registration'
              )}
              {apiApp && (
                <>
                  {' '}and control-plane API app registration (
                  {apiApp.displayName && (
                    <>
                      <code>{apiApp.displayName}</code>,{' '}
                    </>
                  )}
                  client ID <code>{apiApp.clientId}</code>)
                </>
              )}
              .
            </Text>
          </MessageBarBody>
        </MessageBar>
      )}
      <Text block>
        The API managed identity needs tenant-administrator consent for these read-only Microsoft
        Graph application permissions. MOSAIC checks its current token but does not grant consent.
      </Text>
      {status.data && (apiIdentityName || apiIdentityObjectId || apiIdentityAppId || entraApplications.length > 0) && (
        <dl className="directory-identities" aria-label="Microsoft Entra identities and app registrations">
          <div>
            <dt>
              <span>API managed identity (Enterprise application)</span>
              <Text as="span" size={200}>
                Calls Microsoft Graph
              </Text>
            </dt>
            <dd>
              {apiIdentityName && (
                <span>
                  Name: <code data-deployment-identifier="">{apiIdentityName}</code>
                </span>
              )}
              {apiIdentityObjectId && (
                <span>
                  Object (principal) ID: <code>{apiIdentityObjectId}</code>
                </span>
              )}
              {apiIdentityAppId && (
                <span>
                  Application (client) ID: <code>{apiIdentityAppId}</code>
                </span>
              )}
            </dd>
          </div>
          <div>
            <dt>
              <span>Microsoft Graph resource</span>
              <Text as="span" size={200}>
                Target API for app roles
              </Text>
            </dt>
            <dd>
              <span>
                Application ID: <code>{graphAppId}</code>
              </span>
            </dd>
          </div>
          {entraApplications.map((app) => (
            <div key={app.role}>
              <dt>
                <span>{app.label}</span>
                <Text as="span" size={200}>
                  App registration
                </Text>
              </dt>
              <dd>
                {app.displayName && (
                  <span>
                    Name: <code data-deployment-identifier="">{app.displayName}</code>
                  </span>
                )}
                <span>
                  Application (client) ID: <code>{app.clientId}</code>
                </span>
                {app.groupClaimsTarget && (
                  <Badge
                    appearance="outline"
                    color={status.data.groupClaimsEnabled ? 'success' : 'warning'}
                  >
                    {status.data.groupClaimsEnabled ? 'Group claims enabled' : 'Needs group claims'}
                  </Badge>
                )}
              </dd>
            </div>
          ))}
        </dl>
      )}
      {status.data && (
        <dl className="directory-permissions">
          {status.data.permissions.map((permission) => {
            const roleId = permissionRoleId(permission)
            return (
              <div key={permission.permission}>
                <dt>
                  <span>{permission.capability}</span>
                  <Text as="span" size={200}>
                    {permission.required ? 'Required' : 'Optional'}
                  </Text>
                </dt>
                <dd>
                  <code>{permission.permission}</code>
                  {roleId && (
                    <Text as="span" size={200} className="directory-permission-role-id">
                      Role ID: <code>{roleId}</code>
                    </Text>
                  )}
                  <Badge
                    appearance="tint"
                    color={
                      permission.granted === true
                        ? 'success'
                        : permission.granted === false
                          ? permission.required
                            ? 'danger'
                            : 'warning'
                          : 'subtle'
                    }
                  >
                    {permission.granted === true
                      ? 'Granted'
                      : permission.granted === false
                        ? 'Missing'
                        : 'Not checked'}
                  </Badge>
                </dd>
              </div>
            )
          })}
        </dl>
      )}
      {(missingRequired.length > 0 || missingOptional.length > 0 || !permissionsChecked) && status.data?.lookupEnabled && (
        <details className="directory-remediation">
          <summary>How to grant missing permissions</summary>
          <div className="directory-remediation-target">
            <Text block weight="semibold">
              Target identity to authorize in Microsoft Entra
            </Text>
            <Text block size={200}>
              Grant these Microsoft Graph application permissions to the{' '}
              <strong>API managed identity (Enterprise application / Service principal)</strong>
              {apiIdentityName && (
                <>
                  {' '}named <code>{apiIdentityName}</code>
                </>
              )}
              {apiIdentityObjectId && (
                <>
                  {' '}with Object (principal) ID <code>{apiIdentityObjectId}</code>
                </>
              )}
              {apiIdentityAppId && (
                <>
                  {' '}and Application (client) ID <code>{apiIdentityAppId}</code>
                </>
              )}
              . Do not grant them on the{' '}
              {apiApp?.displayName ? <code>{apiApp.displayName}</code> : 'MOSAIC API'} App
              registration—App registrations and system-assigned Managed Identities are separate
              principals in Entra.
            </Text>
          </div>
          <ol>
            <li>
              <strong>Find and inspect in the Azure Portal (Microsoft Entra admin center):</strong>{' '}
              Open <strong>Microsoft Entra admin center &gt; Identity &gt; Applications &gt; Enterprise applications</strong>,
              change the <strong>Application type</strong> filter to <strong>Managed Identities</strong> (or{' '}
              <strong>All Applications</strong>), and search for{' '}
              {apiIdentityName ? <code>{apiIdentityName}</code> : 'the API web app name'}
              {apiIdentityObjectId && (
                <>
                  {' '}(Object ID <code>{apiIdentityObjectId}</code>)
                </>
              )}
              {apiIdentityAppId && (
                <>
                  {' '}or Application ID <code>{apiIdentityAppId}</code>
                </>
              )}
              . Open <strong>Security &gt; Permissions</strong> to review currently granted roles.
              Because Entra ID does not show an <em>Add a permission</em> button on Managed Identity
              blades, open <strong>Azure Cloud Shell (<code>&gt;_</code>)</strong> in the top portal
              bar (or <strong>Microsoft Graph Explorer</strong>) to assign missing app roles.
            </li>
            {missingRequired.length > 0 && (
              <li>
                Ask a Privileged Role Administrator or Global Administrator to run{' '}
                <code>azd provision</code> for this MOSAIC environment. The deployment hook assigns
                the required Graph permissions to the API managed identity.
              </li>
            )}
            {missingRequired.length > 0 && (
              <li>
                If provisioning cannot assign them, run the exact <code>az rest</code> commands
                printed by the deployment hook, or run the pre-filled commands below in Azure CLI or
                Azure Cloud Shell.
              </li>
            )}
            {missingOptional.length > 0 && (
              <li>
                Optional permissions are a separate administrator decision. Assign only the
                optional app roles you need to the API managed identity with Azure CLI or Microsoft
                Graph PowerShell.
              </li>
            )}
            {!permissionsChecked && (
              <li>
                Confirm that the API managed identity can obtain an app-only Microsoft Graph token,
                then retry this check.
              </li>
            )}
            <li>
              Return here and refresh the permission status. Azure App Service caches
              managed-identity tokens until they expire or the API web app is restarted, so newly
              granted permissions may require restarting the API web app before they appear here.
            </li>
          </ol>
          {(missingRequired.length > 0 || missingOptional.length > 0) && (
            <pre className="directory-remediation-code" aria-label="Azure CLI commands to grant missing Graph permissions">
              <code>
                {buildCliCommands(
                  apiIdentityObjectId,
                  apiIdentityName,
                  graphAppId,
                  missingRequired,
                  missingOptional,
                )}
              </code>
            </pre>
          )}
          <Link
            href="https://learn.microsoft.com/en-us/entra/identity/managed-identities-azure-resources/assign-app-role-managed-identity-azure-cli"
            target="_blank"
            rel="noreferrer"
          >
            Microsoft guidance for assigning managed identity app roles
          </Link>
        </details>
      )}
      {status.data?.lookupEnabled && !status.data.groupClaimsEnabled && (
        <details className="directory-remediation">
          <summary>How to enable Entra security-group claims (Portal &amp; CLI)</summary>
          <ol>
            <li>
              <strong>In Microsoft Entra admin center (Azure Portal):</strong> Go to{' '}
              <strong>Identity &gt; Applications &gt; App registrations</strong> and open{' '}
              {groupClaimsTargets.length > 0 ? (
                groupClaimsTargets.map((app, index) => (
                  <span key={app.role}>
                    {index > 0 ? ' and ' : ''}
                    <strong>{app.label}</strong> (
                    {app.displayName && (
                      <>
                        <code>{app.displayName}</code>,{' '}
                      </>
                    )}
                    Application client ID <code>{app.clientId}</code>)
                  </span>
                ))
              ) : (
                <>
                  the <strong>Model-runtime API</strong> and <strong>Control-plane API</strong> app
                  registrations
                </>
              )}
              .
            </li>
            <li>
              In each app registration, select <strong>Manage &gt; Token configuration &gt; Add groups claim</strong>,
              check <strong>Security groups</strong>, and save (this sets{' '}
              <code>groupMembershipClaims</code> to <code>SecurityGroup</code>).
            </li>
            <li>
              Enable group claims in the deployment configuration by running{' '}
              <code>azd env set MOSAIC_ENTRA_GROUP_CLAIMS true</code> and{' '}
              <code>azd provision</code>, or by setting <code>MOSAIC_ENTRA_GROUP_CLAIMS=true</code>{' '}
              on the API web app in Azure Portal.
            </li>
          </ol>
        </details>
      )}
      <Text block size={200}>
        Missing <code>Application.Read.All</code> affects only application and managed-identity
        search. Missing <code>ProfilePhoto.Read.All</code> affects only photos; initials and
        identity icons remain available.
      </Text>
      <Button
        appearance="secondary"
        disabled={status.isFetching}
        onClick={() => void status.refetch()}
      >
        {status.isFetching ? 'Refreshing permission status' : 'Refresh permission status'}
      </Button>
      {status.data && !status.isError && <DirectoryGroupVerification />}
    </section>
  )
}
