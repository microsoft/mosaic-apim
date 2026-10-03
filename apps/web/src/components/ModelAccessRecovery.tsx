import { Button, MessageBar, MessageBarBody, Text } from '@fluentui/react-components'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useMosaicApi } from '../api'
import { ErrorState } from './AsyncState'
import styles from '../pages/EntitlementsPage.module.css'

export function ModelAccessRecovery({
  publicationId,
  runId,
  target = 'model',
}: {
  publicationId: string
  runId?: string | null
  target?: 'model' | 'mcp' | 'pool'
}) {
  const api = useMosaicApi()
  const queryClient = useQueryClient()
  const noun = target === 'pool' ? 'pool' : 'publication'
  const diagnostic = useMutation({
    mutationFn: async () => {
      const lock = target === 'mcp'
        ? await api.getMcpPublicationLock(publicationId)
        : target === 'pool'
          ? await api.getModelPoolLock(publicationId)
          : await api.getPublicationLock(publicationId)
      if (!lock.ownerId) return { run: null }
      // Without confirmQuiesced these only describe the run holding the lock. Nothing is released.
      const recovery = { runId: lock.ownerId, confirmQuiesced: false }
      const run = target === 'mcp'
        ? await api.recoverMcpPublication(publicationId, recovery)
        : target === 'pool'
          ? await api.recoverModelPool(publicationId, recovery)
          : await api.diagnosePublicationRecovery(publicationId, lock.ownerId)
      return { run }
    },
    onSuccess: async () => {
      if (target === 'pool') {
        await queryClient.invalidateQueries({ queryKey: ['model-pools'] })
        return
      }
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: [target === 'mcp' ? 'mcp-publications' : 'publications'] }),
        queryClient.invalidateQueries({ queryKey: ['entitlements'] }),
        queryClient.invalidateQueries({ queryKey: [target === 'mcp' ? 'mcp-publish-run' : 'publish-run', publicationId, runId] }),
      ])
    },
  })

  return (
    <div className={styles.cellStack}>
      <MessageBar intent="warning">
        <MessageBarBody>
          The {noun}&apos;s apply lock may still be retained. An interrupted run status alone does
          not establish recovery or lock release; refresh the {noun} state after operator
          recovery. Before explicitly confirming recovery, an operator must stop the original
          worker and verify that all submitted ARM operations are terminal. Follow the operator
          recovery procedure in README. This UI never confirms quiescence or authorizes releasing
          the lock.
        </MessageBarBody>
      </MessageBar>
      <Button disabled={diagnostic.isPending} onClick={() => diagnostic.mutate()}>
        {diagnostic.isPending ? 'Checking recovery status…' : 'Check recovery status (diagnostic only)'}
      </Button>
      {diagnostic.isError && <ErrorState error={diagnostic.error} />}
      {diagnostic.data && (
        <div className={styles.cellStack}>
          <Text>
            {diagnostic.data.run
              ? `Diagnostic reported run status: ${diagnostic.data.run.status}`
              : `No ${noun} write lock is currently held.`}
          </Text>
          <Text size={200}>No operator confirmation was sent. This is not proof of applied access or revocation.</Text>
          {diagnostic.data.run?.errors.map((error, index) => (
            <Text key={`${index}:${error}`} size={200}>{error}</Text>
          ))}
        </div>
      )}
    </div>
  )
}
