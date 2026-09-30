import assert from 'node:assert/strict'
import { test } from 'node:test'
import { type ApiPrincipal, findWorkloadPrincipal } from '../src/mosaic-api.ts'

const principals: ApiPrincipal[] = [
  { id: 'p_person', objectId: '33333333-0000-0000-0000-000000000001', kind: 'user', label: 'contoso-workload' },
  { id: 'p_group', objectId: '33333333-0000-0000-0000-000000000002', kind: 'securityGroup', label: 'contoso-workload' },
  { id: 'p_relabelled', objectId: '33333333-0000-0000-0000-000000000003', kind: 'application', label: 'Contoso workload' },
  { id: 'p_named', objectId: '33333333-0000-0000-0000-000000000004', kind: 'application', label: 'CONTOSO-WORKLOAD' },
]

test("the workload is found by its object ID first, whatever an admin labelled it", () => {
  const found = findWorkloadPrincipal(principals, { displayName: 'contoso-workload', objectId: '33333333-0000-0000-0000-000000000003' })
  assert.equal(found?.id, 'p_relabelled')
  assert.equal(findWorkloadPrincipal(principals, { displayName: 'contoso-workload', objectId: '33333333-0000-0000-0000-00000000000F' }), undefined)
})

test('without an object ID, only a label equal to the app name matches, and never a person or a group', () => {
  assert.equal(findWorkloadPrincipal(principals, { displayName: 'contoso-workload' })?.id, 'p_named')
  assert.equal(findWorkloadPrincipal(principals.slice(0, 3), { displayName: 'contoso-workload' }), undefined)
  assert.equal(findWorkloadPrincipal(principals, { displayName: 'x', objectId: '33333333-0000-0000-0000-000000000001' }), undefined)
})
