import { readFileSync } from 'node:fs'
import { loadTargets } from '../src/config.ts'
import { modelScopeHash, ModelProofError } from '../src/model-config.ts'
import {
  type IndependentModelEvidence, type RuntimeEvidence, type NotificationDeliveryEvidence,
  governmentEmailEvidence, notificationDeliveryEvidence, propagationEvidence, selectionEvidence,
} from '../src/model-evidence.ts'

// Offline, human-assisted evidence review. No reads from profiles and no calls to any service.
try {
  const [runtimePath, evidencePath] = process.argv.slice(2)
  if (!runtimePath || !evidencePath) throw new ModelProofError('Usage: node tools/model-evidence.ts <local-runtime-report.json> <independent-evidence.json>')
  const targets = loadTargets()
  const scope = targets.modelJourneys
  if (!scope) throw new ModelProofError('Missing isolated modelJourneys')
  const runtime = JSON.parse(readFileSync(runtimePath, 'utf8')) as RuntimeEvidence
  const evidence = JSON.parse(readFileSync(evidencePath, 'utf8')) as IndependentModelEvidence & { notifications?: NotificationDeliveryEvidence }
  let timing: ReturnType<typeof propagationEvidence> | undefined
  if (runtime.scopeSha256 !== modelScopeHash(targets) || evidence.scopeSha256 !== runtime.scopeSha256) throw new ModelProofError('Evidence is not bound to the same reviewed scope')
  if (runtime.journey === 'R10') selectionEvidence(scope, runtime, evidence)
  else if (runtime.journey === 'R12' || runtime.journey === 'R14') {
    timing = propagationEvidence(scope, runtime, evidence)
    if (runtime.journey === 'R14') {
      if (!evidence.notifications) throw new ModelProofError('R14 mailbox/dedupe evidence missing; no-email harness proves only the block/raise leg')
      notificationDeliveryEvidence(evidence.notifications)
    }
  } else if (runtime.journey === 'R13') governmentEmailEvidence(evidence)
  else throw new ModelProofError('Unsupported independent-evidence journey')
  if (timing) console.log(JSON.stringify(timing))
  console.log('Evidence assertions passed for the supplied human-attested captures. This is not a live run or an authenticity check; review the retained original captures before updating a live status.')
} catch (error) {
  // Never echo arbitrary evidence contents, paths, addresses, headers or response bodies on malformed input.
  console.error(error instanceof ModelProofError ? error.message : 'Invalid or unreadable local evidence; assertions did not pass')
  process.exitCode = 1
}
