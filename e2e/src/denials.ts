import type { Response } from '@playwright/test'

/**
 * Denial checks fail on any unexpected success. A recorder notes every MOSAIC API response a page gets while a
 * persona is being turned away; if any of them was a 2xx, MOSAIC served data it should have withheld, whatever
 * the page shows. Only the method, path and status are kept: never a body, header, or query string.
 */

export interface ObservedResponse {
  method: string
  path: string
  status: number
}

/** The responses that should have been refused but succeeded. CORS preflights carry no data and don't count. */
export function unexpectedSuccesses(
  responses: readonly ObservedResponse[],
  allowed: readonly RegExp[] = [],
): ObservedResponse[] {
  return responses.filter(
    (response) =>
      response.method !== 'OPTIONS' &&
      response.status >= 200 &&
      response.status < 300 &&
      !allowed.some((pattern) => pattern.test(response.path)),
  )
}

export function describeResponses(responses: readonly ObservedResponse[]): string {
  return responses.map((response) => `${response.method} ${response.path} returned ${response.status}`).join('; ')
}

/** The API path of a response, when it is a MOSAIC API call. */
export function apiPath(url: string, apiOrigin: string): string | undefined {
  let parsed: URL
  try {
    parsed = new URL(url)
  } catch {
    return undefined
  }
  return parsed.origin === apiOrigin && parsed.pathname.startsWith('/api/') ? parsed.pathname : undefined
}

/** A page or a browser context: anything that reports the responses it receives. */
export interface ResponseSource {
  on(event: 'response', listener: (response: Response) => unknown): unknown
  off(event: 'response', listener: (response: Response) => unknown): unknown
}

/**
 * Records the MOSAIC API responses a page or browser context receives until stop(). Start it on the persona's
 * context before opening the page, so the first load is recorded too.
 */
export class ResponseRecorder {
  readonly #source: ResponseSource
  readonly #apiOrigin: string
  readonly #responses: ObservedResponse[] = []
  readonly #listener = (response: Response) => {
    const path = apiPath(response.url(), this.#apiOrigin)
    if (path !== undefined) this.#responses.push({ method: response.request().method(), path, status: response.status() })
  }

  constructor(source: ResponseSource, apiOrigin: string) {
    this.#source = source
    this.#apiOrigin = apiOrigin
    source.on('response', this.#listener)
  }

  get responses(): readonly ObservedResponse[] {
    return [...this.#responses]
  }

  /** The API calls the page made that were refused, for proving the page did ask. */
  get refusals(): ObservedResponse[] {
    return this.#responses.filter((response) => response.method !== 'OPTIONS' && response.status >= 400)
  }

  unexpectedSuccesses(allowed: readonly RegExp[] = []): ObservedResponse[] {
    return unexpectedSuccesses(this.#responses, allowed)
  }

  stop(): void {
    this.#source.off('response', this.#listener)
  }
}
