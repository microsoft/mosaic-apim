import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { proxy as happyDomProxy } from 'happy-dom/lib/PropertySymbol.js'
import { afterEach, beforeEach } from 'vitest'

// happy-dom has no layout, so document.body measures 0x0 and tabster, Fluent's focus manager, treats
// every element as invisible. An opened Dialog then focuses its own surface instead of its first
// control, tabster never activates the dialog's focus trap, and tabster's delayed aria-hidden pass
// (up to 250 ms after a DOM change) hides the open dialog itself. Give the body a browser-sized box.
// This is a plain property rather than a vi.spyOn() spy, so vi.resetAllMocks() in a test file can't
// quietly put the 0x0 box back.
beforeEach(() => {
  Object.defineProperty(document.body, 'getBoundingClientRect', {
    configurable: true,
    writable: true,
    value: () => new DOMRect(0, 0, 1280, 800),
  })
})
afterEach(() => {
  // Deleting the own property brings back happy-dom's inherited implementation.
  Reflect.deleteProperty(document.body, 'getBoundingClientRect')
})

// happy-dom wraps every <select> in a Proxy, but binds the element's methods to the unwrapped object
// while its focus events carry the Proxy. keyborg, which tabster uses to tell element.focus() calls
// from user focus, compares the two. So when a Dialog's first control is a select, tabster takes the
// Dialog's own focus call for the user's and, as with the 0x0 body, leaves the focus trap off, hides
// the open dialog, and also throws from its focus-restore timer. Focus through the Proxy instead.
Object.defineProperty(HTMLSelectElement.prototype, 'focus', {
  configurable: true,
  writable: true,
  value: function focus(this: HTMLSelectElement, options?: FocusOptions) {
    const select = (this as { [happyDomProxy]?: HTMLSelectElement })[happyDomProxy] ?? this
    HTMLElement.prototype.focus.call(select, options)
  },
})

// Tabster learns that a Dialog opened from MutationObserver records, and turns the dialog's focus trap
// on when focus moves into a dialog it already knows about, or when it finds focus already inside one.
// React's act(), which Testing Library wraps around renders and events, finishes any re-render that an
// effect schedules before those records arrive; a browser delivers them first. So when an opening
// dialog focuses a control that a follow-up render removes, tabster never turns the trap on, and its
// delayed aria-hidden pass hides the open dialog. Hand tabster its pending records whenever focus moves.
const tabsterObservers = new Set<FlushableMutationObserver>()

class FlushableMutationObserver extends MutationObserver {
  readonly #callback: MutationCallback

  constructor(callback: MutationCallback) {
    super(callback)
    this.#callback = callback
  }

  override observe(target: Node, options?: MutationObserverInit) {
    if (options?.attributeFilter?.includes('data-tabster')) tabsterObservers.add(this)
    super.observe(target, options)
  }

  override disconnect() {
    tabsterObservers.delete(this)
    super.disconnect()
  }

  flush() {
    const records = this.takeRecords()
    if (records.length > 0) this.#callback(records, this)
  }
}

globalThis.MutationObserver = FlushableMutationObserver
// Added before any tabster exists, so it runs ahead of tabster's own focus handling.
document.addEventListener('focusin', () => tabsterObservers.forEach((observer) => observer.flush()), true)

// Vitest globals are not enabled, so React Testing Library cannot register its own auto-cleanup.
// Without this, rendered trees accumulate and queries match elements from earlier tests.
afterEach(() => {
  cleanup()
})

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => undefined,
    removeListener: () => undefined,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    dispatchEvent: () => false,
  }),
})
