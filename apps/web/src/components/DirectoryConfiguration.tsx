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
import { ErrorState, Loading } from './AsyncState'
import { DirectoryGroupVerification } from './DirectoryGroupVerification'

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
      <Text block>
        The API managed identity needs tenant-administrator consent for these read-only Microsoft
        Graph application permissions. MOSAIC checks its current token but does not grant consent.
      </Text>
      {status.data && (
        <dl className="directory-permissions">
          {status.data.permissions.map((permission) => (
            <div key={permission.permission}>
              <dt>
                <span>{permission.capability}</span>
                <Text as="span" size={200}>
                  {permission.required ? 'Required' : 'Optional'}
                </Text>
              </dt>
              <dd>
                <code>{permission.permission}</code>
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
          ))}
        </dl>
      )}
      {(missingRequired.length > 0 || missingOptional.length > 0 || !permissionsChecked) && status.data?.lookupEnabled && (
        <details className="directory-remediation">
          <summary>How to grant missing permissions</summary>
          <ol>
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
                printed by the deployment hook.
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
            <li>Return here and refresh the permission status.</li>
          </ol>
          <Link
            href="https://learn.microsoft.com/en-us/entra/identity/managed-identities-azure-resources/assign-app-role-managed-identity-azure-cli"
            target="_blank"
            rel="noreferrer"
          >
            Microsoft guidance for assigning managed identity app roles
          </Link>
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
