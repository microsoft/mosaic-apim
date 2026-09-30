import type { Locator, Page } from '@playwright/test'

export type AriaRole = Parameters<Page['getByRole']>[0]

const ariaRoles = [
  'alert', 'alertdialog', 'application', 'article', 'banner', 'blockquote', 'button', 'caption', 'cell',
  'checkbox', 'code', 'columnheader', 'combobox', 'complementary', 'contentinfo', 'definition', 'deletion',
  'dialog', 'directory', 'document', 'emphasis', 'feed', 'figure', 'form', 'generic', 'grid', 'gridcell',
  'group', 'heading', 'img', 'insertion', 'link', 'list', 'listbox', 'listitem', 'log', 'main', 'marquee',
  'math', 'meter', 'menu', 'menubar', 'menuitem', 'menuitemcheckbox', 'menuitemradio', 'navigation', 'none',
  'note', 'option', 'paragraph', 'presentation', 'progressbar', 'radio', 'radiogroup', 'region', 'row',
  'rowgroup', 'rowheader', 'scrollbar', 'search', 'searchbox', 'separator', 'slider', 'spinbutton', 'status',
  'strong', 'subscript', 'superscript', 'switch', 'tab', 'table', 'tablist', 'tabpanel', 'term', 'textbox',
  'time', 'timer', 'toolbar', 'tooltip', 'tree', 'treegrid', 'treeitem',
] as const satisfies readonly AriaRole[]

const textKinds = ['label', 'text', 'placeholder', 'testid', 'css', 'title', 'alt'] as const

export type TextKind = (typeof textKinds)[number]

export type ParsedSpec =
  | { kind: 'role'; role: AriaRole; name?: string | RegExp }
  | { kind: TextKind; value: string | RegExp }

export interface LocatorOptions {
  exact?: boolean
  nth?: number
  within?: string
  row?: string
  hasText?: string
}

export class LocatorSpecError extends Error {}

function toMatcher(raw: string): string | RegExp {
  const regex = /^\/(.+)\/([dgimsuy]*)$/s.exec(raw)
  if (!regex) return raw
  try {
    return new RegExp(regex[1], regex[2].replace('g', ''))
  } catch (error) {
    throw new LocatorSpecError(`Invalid regular expression ${raw}: ${(error as Error).message}`)
  }
}

export function parseSpec(spec: string): ParsedSpec {
  const separator = spec.indexOf(':')
  if (separator <= 0) {
    throw new LocatorSpecError(
      `Locator "${spec}" must start with role:, label:, text:, placeholder:, testid:, css:, title:, or alt:`,
    )
  }
  const kind = spec.slice(0, separator)
  const rest = spec.slice(separator + 1)
  if (kind === 'role') {
    const nameSeparator = rest.indexOf(':')
    const role = nameSeparator === -1 ? rest : rest.slice(0, nameSeparator)
    if (!(ariaRoles as readonly string[]).includes(role)) {
      throw new LocatorSpecError(`Unknown ARIA role "${role}" in locator "${spec}"`)
    }
    const name = nameSeparator === -1 ? undefined : rest.slice(nameSeparator + 1)
    return name === undefined || name === ''
      ? { kind: 'role', role: role as AriaRole }
      : { kind: 'role', role: role as AriaRole, name: toMatcher(name) }
  }
  if (!(textKinds as readonly string[]).includes(kind)) {
    throw new LocatorSpecError(`Unknown locator kind "${kind}" in "${spec}"`)
  }
  if (rest === '') throw new LocatorSpecError(`Locator "${spec}" needs a value after "${kind}:"`)
  return { kind: kind as TextKind, value: kind === 'css' || kind === 'testid' ? rest : toMatcher(rest) }
}

type Root = Page | Locator

function resolveParsed(root: Root, parsed: ParsedSpec, exact: boolean | undefined): Locator {
  switch (parsed.kind) {
    case 'role':
      return parsed.name === undefined
        ? root.getByRole(parsed.role)
        : root.getByRole(parsed.role, { name: parsed.name, exact })
    case 'label':
      return root.getByLabel(parsed.value, { exact })
    case 'text':
      return root.getByText(parsed.value, { exact })
    case 'placeholder':
      return root.getByPlaceholder(parsed.value, { exact })
    case 'title':
      return root.getByTitle(parsed.value, { exact })
    case 'alt':
      return root.getByAltText(parsed.value, { exact })
    case 'testid':
      return root.getByTestId(parsed.value)
    case 'css':
      return root.locator(parsed.value as string)
  }
}

export function locate(root: Root, spec: string, options: LocatorOptions = {}): Locator {
  let scope: Root = root
  if (options.within) scope = resolveParsed(scope, parseSpec(options.within), options.exact).first()
  if (options.row) scope = scope.getByRole('row').filter({ hasText: toMatcher(options.row) }).first()
  let locator = resolveParsed(scope, parseSpec(spec), options.exact)
  if (options.hasText) locator = locator.filter({ hasText: toMatcher(options.hasText) })
  if (options.nth !== undefined) {
    if (!Number.isInteger(options.nth) || options.nth < -1) {
      throw new LocatorSpecError('nth must be a whole number of 0 or more, or -1 for the last match')
    }
    locator = options.nth === -1 ? locator.last() : locator.nth(options.nth)
  }
  return locator
}

export function describeLocator(spec: string, options: LocatorOptions = {}): string {
  const parts = [spec]
  if (options.within) parts.push(`within ${options.within}`)
  if (options.row) parts.push(`in row "${options.row}"`)
  if (options.hasText) parts.push(`having "${options.hasText}"`)
  if (options.nth !== undefined) parts.push(`#${options.nth}`)
  if (options.exact) parts.push('(exact)')
  return parts.join(' ')
}
