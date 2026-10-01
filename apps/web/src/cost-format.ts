/** Money in the console: US dollars at list price, and never $0 for something MOSAIC can't price. */

const dollars = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' })
const compactDollars = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  notation: 'compact',
  maximumFractionDigits: 1,
})

export const NO_PRICE = 'No price'

/** A cost to the cent. Under a cent shows as "<$0.01", so a priced trickle never reads as free. */
export function formatCost(value: number | null | undefined, missing = NO_PRICE): string {
  if (value == null) return missing
  if (value > 0 && value < 0.01) return '<$0.01'
  return dollars.format(value)
}

/** A headline cost: compact once it reaches $10,000, so a KPI stays one short figure. */
export function formatCostCompact(value: number | null | undefined, missing = NO_PRICE): string {
  if (value == null) return missing
  if (Math.abs(value) >= 10_000) return compactDollars.format(value)
  return formatCost(value, missing)
}

/** A list price, which can run to fractions of a cent per million tokens. */
export function formatRate(value: number | null | undefined, missing = '—'): string {
  if (value == null) return missing
  const digits = value !== 0 && Math.abs(value) < 0.1 ? 4 : 2
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: 2,
    maximumFractionDigits: digits,
  }).format(value)
}

export function formatShare(value: number | null | undefined): string {
  if (value == null) return '—'
  return `${(value * 100).toFixed(1)}%`
}
