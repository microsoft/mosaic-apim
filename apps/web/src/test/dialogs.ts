import { configure, getConfig } from '@testing-library/react'
import { afterAll, beforeAll } from 'vitest'

/**
 * Makes role queries in the calling describe block include aria-hidden nodes.
 *
 * happy-dom has no layout, so tabster treats every element as invisible and Fluent focuses an opened
 * dialog's surface before tabster registers the dialog's focus trap. The trap never activates, and
 * tabster's next DOM sweep, about 250 ms after any change, marks the open dialog aria-hidden. Without
 * this, a role query that runs after that sweep cannot find the dialog, and the test fails whenever the
 * machine is slow.
 */
export function includeAriaHiddenInRoleQueries() {
  let previous = false
  beforeAll(() => {
    previous = getConfig().defaultHidden
    configure({ defaultHidden: true })
  })
  afterAll(() => {
    configure({ defaultHidden: previous })
  })
}
