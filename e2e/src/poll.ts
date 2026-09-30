/**
 * Polling for outcomes MOSAIC and API Management reach asynchronously, such as a plan finishing its apply or a
 * revocation propagating. The probe says whether it is done; if not, it can describe what it saw, and the
 * timeout error reports the last description. Descriptions must be static text or MOSAIC names: never a key,
 * a token, or anything a response carried that could be one.
 */

export type PollResult<T> = { done: true; value: T } | { done: false; state?: string }

export const done = <T>(value: T): PollResult<T> => ({ done: true, value })
export const pending = <T = never>(state?: string): PollResult<T> => ({ done: false, state })

export interface PollOptions {
  timeoutMs: number
  intervalMs: number
  now?: () => number
  sleep?: (ms: number) => Promise<void>
}

export class PollTimeout extends Error {
  readonly lastState?: string

  constructor(message: string, lastState?: string) {
    super(message)
    this.name = 'PollTimeout'
    this.lastState = lastState
  }
}

const defaultSleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms))

/**
 * Calls probe until it reports done, and returns its value. It probes once more at the deadline, then throws a
 * PollTimeout naming what it waited for. Errors from the probe end the wait at once.
 */
export async function pollUntil<T>(
  what: string,
  probe: (attempt: number) => Promise<PollResult<T>> | PollResult<T>,
  options: PollOptions,
): Promise<T> {
  const { timeoutMs, intervalMs, now = Date.now, sleep = defaultSleep } = options
  if (!Number.isFinite(timeoutMs) || timeoutMs < 0) throw new RangeError('timeoutMs must be zero or more')
  if (!Number.isFinite(intervalMs) || intervalMs <= 0) throw new RangeError('intervalMs must be more than zero')
  const started = now()
  let lastState: string | undefined
  for (let attempt = 1; ; attempt += 1) {
    const result = await probe(attempt)
    if (result.done) return result.value
    if (result.state !== undefined) lastState = result.state
    const elapsed = now() - started
    if (elapsed >= timeoutMs) {
      const seconds = Math.round(timeoutMs / 1_000)
      const last = lastState === undefined ? '' : ` Last seen: ${lastState}.`
      throw new PollTimeout(`Waited ${seconds} seconds for ${what}.${last}`, lastState)
    }
    await sleep(Math.min(intervalMs, timeoutMs - elapsed))
  }
}
