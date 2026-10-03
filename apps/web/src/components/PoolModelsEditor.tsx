import { Button, Checkbox, Field, Input, Select, Switch, Text } from '@fluentui/react-components'
import { AddRegular, ArrowDownRegular, ArrowUpRegular, DeleteRegular } from '@fluentui/react-icons'
import { useState } from 'react'
import { CAPACITY_TYPE_LABELS, plural } from '../labels'
import {
  MAX_POOL_WEIGHT,
  MIN_POOL_WEIGHT,
  candidateKey,
  draftFromCandidate,
  keyedTurn,
  memberKey,
  poolFamilyLabel,
  preferentialPriority,
  publicNameProblem,
  sameFamily,
  suggestedWeights,
} from '../pools'
import type { DraftPoolMember, DraftPoolModel, PoolFamily } from '../pools'
import type {
  EnvironmentCatalogView,
  ModelPoolType,
  PoolCandidateDeployment,
  PoolCandidateModel,
} from '../types'
import { EnvironmentBadge } from './EnvironmentBadge'
import { PoolMemberAccessBadges, PoolReadinessBadge } from './PoolBadges'
import styles from './PoolDialogs.module.css'

function familyOf(drafts: DraftPoolModel[], locked?: PoolFamily | null): PoolFamily | null {
  if (locked) return locked
  const first = drafts[0]
  return first ? { vendor: first.modelFormat, apiShape: first.apiShape } : null
}

/** Why a model can't be added to the pool now, or null when it can. */
function unavailableReason(model: PoolCandidateModel, drafts: DraftPoolModel[], family: PoolFamily | null): string | null {
  const key = candidateKey(model)
  if (drafts.some((draft) => draft.candidateKey === key)) return 'already in this pool'
  if (family && !sameFamily(model, family)) return 'a different vendor or API'
  if (!model.deployments.some((deployment) => deployment.eligible)) return 'no deployment can join'
  return null
}

function familyLabel(model: Pick<PoolCandidateModel, 'modelFormat' | 'apiShape'>): string {
  return poolFamilyLabel(model.modelFormat, model.apiShape)
}

function memberLabel(member: Pick<DraftPoolMember, 'deploymentName'>, deployment?: PoolCandidateDeployment): string {
  return deployment ? `${member.deploymentName} on ${deployment.endpointName}` : member.deploymentName
}

/** How a breaker or preferential pool tries the active deployments it reaches with an API key. */
function keyedNote(keyed: number, afterBackendPool: boolean): string {
  if (!afterBackendPool) {
    return keyed === 1
      ? 'The only active deployment is reached with an API key, so each request tries it once.'
      : 'Every active deployment is reached with an API key, so requests try each one once, in the order listed.'
  }
  return keyed === 1
    ? 'A deployment reached with an API key can’t join the backend pool, so it’s tried once, after the others.'
    : 'Deployments reached with an API key can’t join the backend pool, so each is tried once, in the order listed, after the others.'
}

interface MemberRow {
  key: string
  member: DraftPoolMember | null
  memberIndex: number
  deployment?: PoolCandidateDeployment
}

function PoolModelCard({
  draft,
  candidate,
  poolType,
  otherNames,
  poolId,
  catalog,
  disabled,
  onChange,
  onRemove,
}: {
  draft: DraftPoolModel
  candidate?: PoolCandidateModel
  poolType: ModelPoolType
  otherNames: string[]
  poolId?: string
  catalog?: EnvironmentCatalogView
  disabled?: boolean
  onChange: (draft: DraftPoolModel) => void
  onRemove: () => void
}) {
  const deployments = new Map(
    (candidate?.deployments ?? []).map((deployment) => [memberKey(deployment.modelEndpointId, deployment.deploymentName), deployment]),
  )
  const included = new Set(draft.members.map((member) => memberKey(member.modelEndpointId, member.deploymentName)))
  const rows: MemberRow[] = [
    ...draft.members.map((member, memberIndex) => {
      const key = memberKey(member.modelEndpointId, member.deploymentName)
      return { key, member, memberIndex, deployment: deployments.get(key) }
    }),
    ...[...deployments.entries()]
      .filter(([key]) => !included.has(key))
      .map(([key, deployment]) => ({ key, member: null, memberIndex: -1, deployment })),
  ]
  const versions = new Set(
    rows
      .filter((row) => row.member)
      .map((row) => row.deployment?.modelVersion)
      .filter((version): version is string => Boolean(version)),
  )
  const nameProblem = publicNameProblem(draft.publicName, otherNames)
  const title = draft.displayName.trim() || draft.modelName

  const setMembers = (members: DraftPoolMember[]) => onChange({ ...draft, members })
  const updateMember = (index: number, change: Partial<DraftPoolMember>) =>
    setMembers(draft.members.map((member, position) => (position === index ? { ...member, ...change } : member)))
  const move = (index: number, offset: -1 | 1) => {
    const members = [...draft.members]
    const [moved] = members.splice(index, 1)
    members.splice(index + offset, 0, moved)
    setMembers(members)
  }
  const swap = (left: number, right: number) =>
    setMembers(
      draft.members.map((member, index) =>
        index === left ? draft.members[right] : index === right ? draft.members[left] : member,
      ),
    )
  // A breaker or preferential pool can't put a deployment it reaches with an API key in its backend
  // pool, so it tries each active one once, in the order they're listed, after the backend pool.
  const keyedOf = (member: DraftPoolMember) =>
    poolType !== 'linear' && deployments.get(memberKey(member.modelEndpointId, member.deploymentName))?.apiKey === true
  const keyedActive = draft.members.flatMap((member, index) => (keyedOf(member) && !member.drained ? [index] : []))
  const afterBackendPool = draft.members.some((member) => !member.drained && !keyedOf(member))
  const weighted = draft.members.filter((member) => !keyedOf(member)).length
  const include = (deployment: PoolCandidateDeployment) =>
    setMembers([
      ...draft.members,
      { modelEndpointId: deployment.modelEndpointId, deploymentName: deployment.deploymentName, weight: 1, drained: false },
    ])
  const suggestWeights = () => {
    const weights = suggestedWeights(
      draft.members.map((member) => {
        const deployment = deployments.get(memberKey(member.modelEndpointId, member.deploymentName))
        return {
          capacityType: deployment?.capacityType ?? 'unknown',
          skuCapacity: deployment?.skuCapacity ?? null,
          apiKey: deployment?.apiKey,
        }
      }),
      poolType,
    )
    setMembers(draft.members.map((member, index) => ({ ...member, weight: weights[index] })))
  }

  return (
    <section className={styles.modelCard} aria-label={title}>
      <div className={styles.modelHeader}>
        <div className={styles.modelHeading}>
          <h3>{title}</h3>
          <Text size={200} className={styles.muted}>
            {draft.modelName} · {familyLabel(draft)} · {plural(draft.members.length, 'deployment')}
          </Text>
        </div>
        <Button appearance="subtle" icon={<DeleteRegular />} onClick={onRemove} disabled={disabled} aria-label={`Remove ${title}`}>
          Remove
        </Button>
      </div>
      <div className={styles.fieldRow}>
        <Field
          label="Name callers send"
          required
          validationState={nameProblem ? 'error' : 'none'}
          validationMessage={nameProblem ?? undefined}
          hint={nameProblem ? undefined : 'Callers use this name to pick the model. It needn’t match any deployment.'}
        >
          <Input
            value={draft.publicName}
            disabled={disabled}
            onChange={(_, data) => onChange({ ...draft, publicName: data.value })}
          />
        </Field>
        <Field label="Display name" hint="What people see in the console and the portal">
          <Input
            value={draft.displayName}
            disabled={disabled}
            onChange={(_, data) => onChange({ ...draft, displayName: data.value })}
          />
        </Field>
      </div>
      <div className={styles.modelOptions}>
        <Checkbox
          checked={draft.listed}
          disabled={disabled}
          label="List in the portal catalog"
          onChange={(_, data) => onChange({ ...draft, listed: data.checked === true })}
        />
        {(versions.size > 1 || draft.allowMixedVersions) && (
          <Checkbox
            checked={draft.allowMixedVersions}
            disabled={disabled}
            label={`Allow mixed versions (${[...versions].sort().join(', ') || 'none chosen'})`}
            onChange={(_, data) => onChange({ ...draft, allowMixedVersions: data.checked === true })}
          />
        )}
      </div>
      {rows.length === 0 ? (
        <Text className={styles.muted}>MOSAIC doesn’t find any deployment of {draft.modelName} now.</Text>
      ) : (
        <div className={`table-scroll ${styles.tableScroll}`}>
          <table>
            <thead>
              <tr>
                <th scope="col">Use</th>
                <th scope="col">Deployment</th>
                <th scope="col">Capacity</th>
                <th scope="col">Readiness</th>
                <th scope="col">{poolType === 'linear' ? 'Order' : 'Weight'}</th>
                <th scope="col">Drain</th>
              </tr>
            </thead>
            <tbody>
              {rows.map(({ key, member, memberIndex, deployment }) => {
                const name = memberLabel(member ?? { deploymentName: deployment?.deploymentName ?? '' }, deployment)
                const otherPools = (deployment?.poolIds ?? []).filter((id) => id !== poolId).length
                const keyed = member != null && keyedOf(member)
                const turn = keyed ? keyedActive.indexOf(memberIndex) : -1
                return (
                  <tr key={key}>
                    <td>
                      <Checkbox
                        aria-label={`Use ${name}`}
                        checked={member != null}
                        disabled={disabled || (member == null && !deployment?.eligible)}
                        onChange={(_, data) => {
                          if (data.checked === true && deployment) include(deployment)
                          else if (member) setMembers(draft.members.filter((_, index) => index !== memberIndex))
                        }}
                      />
                    </td>
                    <td>
                      <div className={styles.cellStack}>
                        <strong>{deployment?.endpointName ?? 'Endpoint not found'}</strong>
                        <Text size={200} className={styles.muted}>
                          {[
                            member?.deploymentName ?? deployment?.deploymentName,
                            deployment?.region,
                            deployment?.modelVersion ? `version ${deployment.modelVersion}` : null,
                          ]
                            .filter(Boolean)
                            .join(' · ')}
                        </Text>
                        {deployment && (
                          <span className={styles.accessBadges}>
                            <EnvironmentBadge environment={deployment.environment ?? null} catalog={catalog} size="small" />
                            <PoolMemberAccessBadges
                              apiKey={deployment.apiKey}
                              declared={deployment.declared}
                              provider={deployment.provider}
                            />
                          </span>
                        )}
                        {deployment && !deployment.eligible && deployment.reason && (
                          <Text size={200} className={styles.caution}>{deployment.reason}</Text>
                        )}
                        {deployment?.eligible && deployment.environmentVerdict.level === 'warning' && (
                          <Text size={200} className={styles.caution}>{deployment.environmentVerdict.reason}</Text>
                        )}
                        {!deployment && (
                          <Text size={200} className={styles.caution}>
                            MOSAIC no longer finds this deployment. Remove it, or sync its endpoint.
                          </Text>
                        )}
                        {otherPools > 0 && (
                          <Text size={200} className={styles.muted}>Also in {plural(otherPools, 'other pool')}</Text>
                        )}
                      </div>
                    </td>
                    <td>
                      <div className={styles.cellStack}>
                        <span>{CAPACITY_TYPE_LABELS[deployment?.capacityType ?? 'unknown']}</span>
                        {deployment?.skuName && (
                          <Text size={200} className={styles.muted}>
                            {deployment.skuName}
                            {deployment.skuCapacity != null ? ` ${deployment.skuCapacity}` : ''}
                          </Text>
                        )}
                        {deployment?.spilloverDeploymentName && (
                          <Text size={200} className={styles.muted}>
                            Spills over to {deployment.spilloverDeploymentName}
                          </Text>
                        )}
                      </div>
                    </td>
                    <td>
                      {deployment ? (
                        <PoolReadinessBadge readiness={deployment.readiness} />
                      ) : (
                        <Text className={styles.muted}>Unknown</Text>
                      )}
                    </td>
                    <td>
                      {member && poolType === 'linear' && (
                        <div className={styles.orderCell}>
                          <span className={styles.orderNumber}>{memberIndex + 1}</span>
                          <Button
                            appearance="subtle"
                            size="small"
                            icon={<ArrowUpRegular />}
                            aria-label={`Move ${name} up`}
                            disabled={disabled || memberIndex === 0}
                            onClick={() => move(memberIndex, -1)}
                          />
                          <Button
                            appearance="subtle"
                            size="small"
                            icon={<ArrowDownRegular />}
                            aria-label={`Move ${name} down`}
                            disabled={disabled || memberIndex === draft.members.length - 1}
                            onClick={() => move(memberIndex, 1)}
                          />
                        </div>
                      )}
                      {member && poolType !== 'linear' && !keyed && (
                        <div className={styles.cellStack}>
                          <Input
                            className={styles.weightInput}
                            type="number"
                            min={MIN_POOL_WEIGHT}
                            max={MAX_POOL_WEIGHT}
                            aria-label={`Weight for ${name}`}
                            disabled={disabled}
                            value={member.weight ? String(member.weight) : ''}
                            onChange={(_, data) => updateMember(memberIndex, { weight: data.value === '' ? 0 : Number(data.value) })}
                          />
                          {poolType === 'preferential' && (
                            <Text size={200} className={styles.muted}>
                              {preferentialPriority(deployment?.capacityType ?? 'unknown') === 1 ? 'Tried first' : 'Overflow'}
                            </Text>
                          )}
                        </div>
                      )}
                      {member && keyed && (
                        <div className={styles.cellStack}>
                          <Text size={200}>
                            {keyedTurn(turn >= 0 ? turn + 1 : null, keyedActive.length, afterBackendPool)}
                          </Text>
                          {turn >= 0 && keyedActive.length > 1 && (
                            <div className={styles.orderCell}>
                              <Button
                                appearance="subtle"
                                size="small"
                                icon={<ArrowUpRegular />}
                                aria-label={`Move ${name} up`}
                                disabled={disabled || turn === 0}
                                onClick={() => swap(memberIndex, keyedActive[turn - 1])}
                              />
                              <Button
                                appearance="subtle"
                                size="small"
                                icon={<ArrowDownRegular />}
                                aria-label={`Move ${name} down`}
                                disabled={disabled || turn === keyedActive.length - 1}
                                onClick={() => swap(memberIndex, keyedActive[turn + 1])}
                              />
                            </div>
                          )}
                        </div>
                      )}
                    </td>
                    <td>
                      {member && (
                        <Switch
                          aria-label={`Drain ${name}`}
                          checked={member.drained}
                          disabled={disabled}
                          onChange={(_, data) => updateMember(memberIndex, { drained: data.checked })}
                        />
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
      <div className={styles.modelFooter}>
        <Text size={200} className={styles.muted}>
          {poolType === 'linear'
            ? 'Requests try the deployments from the top. A drained deployment stays in the pool but gets no requests.'
            : poolType === 'preferential'
              ? 'Requests go to provisioned deployments first, by weight. Pay-as-you-go deployments take the overflow. A drained deployment gets no requests.'
              : 'Requests spread across the deployments by weight. A drained deployment stays in the pool but gets no requests.'}
          {keyedActive.length > 0 && ` ${keyedNote(keyedActive.length, afterBackendPool)}`}
        </Text>
        {poolType !== 'linear' && weighted > 1 && (
          <Button size="small" onClick={suggestWeights} disabled={disabled}>
            Suggest weights from capacity
          </Button>
        )}
      </div>
    </section>
  )
}

/** Choose a pool's models and, for each, the deployments that serve it. */
export function PoolModelsEditor({
  candidates,
  poolType,
  drafts,
  onChange,
  poolId,
  locked,
  catalog,
  disabled,
}: {
  candidates: PoolCandidateModel[]
  poolType: ModelPoolType
  drafts: DraftPoolModel[]
  onChange: (drafts: DraftPoolModel[]) => void
  poolId?: string
  /** The vendor and API a published pool is fixed to. */
  locked?: PoolFamily | null
  catalog?: EnvironmentCatalogView
  disabled?: boolean
}) {
  const [selected, setSelected] = useState('')
  const family = familyOf(drafts, locked)
  const byKey = new Map(candidates.map((model) => [candidateKey(model), model]))
  const groups = new Map<string, PoolCandidateModel[]>()
  for (const model of [...candidates].sort((left, right) => left.modelName.localeCompare(right.modelName))) {
    const label = familyLabel(model)
    groups.set(label, [...(groups.get(label) ?? []), model])
  }
  const selectedModel = byKey.get(selected)
  const canAdd = selectedModel != null && unavailableReason(selectedModel, drafts, family) == null

  const add = () => {
    if (!selectedModel || !canAdd) return
    onChange([...drafts, draftFromCandidate(selectedModel, poolType)])
    setSelected('')
  }

  return (
    <div className={styles.form}>
      <Text>
        {family
          ? `This pool serves ${poolFamilyLabel(family.vendor, family.apiShape)} models. Add more of them, and choose the deployments behind each.`
          : 'A pool serves one vendor’s models through one API. The first model you add sets both.'}
      </Text>
      <div className={styles.addModel}>
        <Field label="Add a model" className={styles.addModelField}>
          <Select value={selected} disabled={disabled} onChange={(event) => setSelected(event.target.value)}>
            <option value="">Choose a model</option>
            {[...groups.entries()].map(([label, models]) => (
              <optgroup key={label} label={label}>
                {models.map((model) => {
                  const reason = unavailableReason(model, drafts, family)
                  const eligible = model.deployments.filter((deployment) => deployment.eligible).length
                  return (
                    <option key={candidateKey(model)} value={candidateKey(model)} disabled={reason != null}>
                      {reason
                        ? `${model.modelName} (${reason})`
                        : `${model.modelName} (${eligible} of ${plural(model.deployments.length, 'deployment')} can join)`}
                    </option>
                  )
                })}
              </optgroup>
            ))}
          </Select>
        </Field>
        <Button icon={<AddRegular />} onClick={add} disabled={disabled || !canAdd}>
          Add model
        </Button>
      </div>
      {candidates.length === 0 && (
        <Text className={styles.muted}>
          MOSAIC hasn’t found any model deployments yet. Connect a model endpoint and sync it first.
        </Text>
      )}
      {drafts.map((draft, index) => (
        <PoolModelCard
          key={`${index}:${draft.candidateKey}`}
          draft={draft}
          candidate={byKey.get(draft.candidateKey)}
          poolType={poolType}
          otherNames={drafts.filter((_, other) => other !== index).map((other) => other.publicName)}
          poolId={poolId}
          catalog={catalog}
          disabled={disabled}
          onChange={(next) => onChange(drafts.map((current, position) => (position === index ? next : current)))}
          onRemove={() => onChange(drafts.filter((_, position) => position !== index))}
        />
      ))}
    </div>
  )
}
