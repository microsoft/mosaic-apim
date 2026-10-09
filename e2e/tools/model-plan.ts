import { loadTargets } from '../src/config.ts'
import { modelReadiness, modelScopeHash } from '../src/model-config.ts'

// Offline only: no browsers, tokens, HTTP, or writes. Do not populate approval without the owner's decision.
const targets = loadTargets()
console.log(JSON.stringify({
  scopeSha256: modelScopeHash(targets),
  readiness: Object.fromEntries((['R10', 'R11', 'R12', 'R14'] as const).map((j) => [j, modelReadiness(targets, j, process.env)])),
  R13: 'BLOCKED: human-assisted Government fixture, MI send and actual mailbox/dedupe evidence required',
}, null, 2))
