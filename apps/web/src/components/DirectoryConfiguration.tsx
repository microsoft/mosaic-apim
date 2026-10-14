import { Button, MessageBar, MessageBarBody, Text, Title3 } from '@fluentui/react-components'
import { useQuery } from '@tanstack/react-query'
import { useMosaicApi } from '../api'
import { ErrorState, Loading } from './AsyncState'

export function DirectoryConfiguration() {
  const api = useMosaicApi()
  const status = useQuery({ queryKey: ['directory', 'status'], queryFn: api.getDirectoryStatus })
  return (
    <section className="directory-configuration">
      <Title3 as="h3">Microsoft Entra directory</Title3>
      {status.isPending ? <Loading label="Checking directory configuration" /> : status.isError ? (
        <ErrorState title="Directory configuration unavailable" error={status.error} />
      ) : (
        <MessageBar intent={status.data.lookupEnabled ? 'info' : 'warning'}>
          <MessageBarBody>
            {status.data.lookupEnabled
              ? 'Directory lookup is available automatically with Entra authentication. A successful search confirms access for that identity type; availability alone does not verify Graph consent.'
              : 'Directory lookup requires Entra authentication and is unavailable with local authentication. Use manual entry to record identities without directory verification.'}
          </MessageBarBody>
        </MessageBar>
      )}
      <Text block>The API identity needs tenant-administrator consent for these read-only Microsoft Graph permissions. MOSAIC does not grant them.</Text>
      <dl className="directory-permissions">
        <div><dt>People and agent users</dt><dd><code>User.ReadBasic.All</code></dd></div>
        <div><dt>Entra security groups and members</dt><dd><code>GroupMember.Read.All</code></dd></div>
        <div><dt>Entra agent identities</dt><dd><code>AgentIdentity.Read.All</code></dd></div>
        <div><dt>Applications and managed identities</dt><dd><code>Application.Read.All</code></dd></div>
        <div><dt>Optional people and administrator photos</dt><dd><code>ProfilePhoto.Read.All</code></dd></div>
      </dl>
      <Text block size={200}>Missing photo consent does not prevent search or registration. Initials and identity icons remain available.</Text>
      <Button appearance="secondary" disabled={status.isFetching} onClick={() => void status.refetch()}>Refresh directory configuration</Button>
    </section>
  )
}
