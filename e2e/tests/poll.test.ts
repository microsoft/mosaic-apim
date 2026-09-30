import assert from 'node:assert/strict'
import { test } from 'node:test'
import { PollTimeout, done, pending, pollUntil } from '../src/poll.ts'

function fakeClock() {
  let time = 0
  const sleeps: number[] = []
  return {
    now: () => time,
    sleep: async (ms: number) => {
      sleeps.push(ms)
      time += ms
    },
    sleeps,
    advance: (ms: number) => {
      time += ms
    },
  }
}

test('returns the first value a probe reports, without sleeping', async () => {
  const clock = fakeClock()
  assert.equal(await pollUntil('a value', () => done(7), { timeoutMs: 1_000, intervalMs: 100, ...clock }), 7)
  assert.deepEqual(clock.sleeps, [])
})

test('probes at each interval until the outcome arrives', async () => {
  const clock = fakeClock()
  const seen: number[] = []
  const value = await pollUntil(
    'the plan to apply',
    (attempt) => {
      seen.push(attempt)
      return attempt < 3 ? pending('applying') : done('applied')
    },
    { timeoutMs: 10_000, intervalMs: 1_000, ...clock },
  )
  assert.equal(value, 'applied')
  assert.deepEqual(seen, [1, 2, 3])
  assert.deepEqual(clock.sleeps, [1_000, 1_000])
})

test('probes once more at the deadline, then names what it waited for and what it last saw', async () => {
  const clock = fakeClock()
  let attempts = 0
  await assert.rejects(
    pollUntil(
      'the grant to be revoked',
      () => {
        attempts += 1
        return pending(attempts === 1 ? 'applying' : undefined)
      },
      { timeoutMs: 2_500, intervalMs: 1_000, ...clock },
    ),
    (error: unknown) => {
      assert.ok(error instanceof PollTimeout)
      assert.equal(error.message, 'Waited 3 seconds for the grant to be revoked. Last seen: applying.')
      assert.equal(error.lastState, 'applying')
      return true
    },
  )
  assert.deepEqual(clock.sleeps, [1_000, 1_000, 500])
  assert.equal(attempts, 4)
})

test('a slow probe counts against the deadline', async () => {
  const clock = fakeClock()
  await assert.rejects(
    pollUntil(
      'a slow answer',
      () => {
        clock.advance(5_000)
        return pending()
      },
      { timeoutMs: 4_000, intervalMs: 1_000, ...clock },
    ),
    /^PollTimeout: Waited 4 seconds for a slow answer\.$/,
  )
  assert.deepEqual(clock.sleeps, [])
})

test('probe errors end the wait at once', async () => {
  const clock = fakeClock()
  await assert.rejects(
    pollUntil(
      'anything',
      () => {
        throw new Error('403 from /api/v1/publications')
      },
      { timeoutMs: 10_000, intervalMs: 1_000, ...clock },
    ),
    /403 from/,
  )
  assert.deepEqual(clock.sleeps, [])
})

test('refuses intervals and timeouts that would never end or spin', async () => {
  await assert.rejects(pollUntil('x', () => done(1), { timeoutMs: -1, intervalMs: 1 }), RangeError)
  await assert.rejects(pollUntil('x', () => done(1), { timeoutMs: 1, intervalMs: 0 }), RangeError)
  await assert.rejects(pollUntil('x', () => done(1), { timeoutMs: Number.NaN, intervalMs: 1 }), RangeError)
})
