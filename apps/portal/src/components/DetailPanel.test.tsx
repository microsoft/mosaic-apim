import { FluentProvider, webLightTheme } from '@fluentui/react-components'
import { act, render, screen, within } from '@testing-library/react'
import { StrictMode } from 'react'
import { afterEach, expect, it, vi } from 'vitest'
import { DetailPanel } from './DetailPanel'

afterEach(() => vi.restoreAllMocks())

it('labels the dialog with its heading, not the heading actions', () => {
  render(<FluentProvider theme={webLightTheme}>
    <DetailPanel title="Access to model" onClose={vi.fn()}>Grant details</DetailPanel>
  </FluentProvider>)
  const dialog = screen.getByRole('dialog', { name: 'Access to model' })
  expect(dialog).toHaveAttribute('aria-labelledby', within(dialog).getByRole('heading', { name: 'Access to model' }).id)
  expect(dialog).toHaveAccessibleName('Access to model')
})

it('cancels StrictMode restoration while mounted and restores the opener on unmount', () => {
  const frames = new Map<number, FrameRequestCallback>()
  let nextFrame = 0
  vi.spyOn(globalThis, 'requestAnimationFrame').mockImplementation((callback) => {
    frames.set(++nextFrame, callback)
    return nextFrame
  })
  vi.spyOn(globalThis, 'cancelAnimationFrame').mockImplementation((frame) => {
    frames.delete(frame)
  })
  const flushFrames = () => act(() => {
    const callbacks = [...frames.values()]
    frames.clear()
    for (const callback of callbacks) callback(performance.now())
  })
  render(<button>View access</button>)
  const opener = screen.getByRole('button', { name: 'View access' })
  opener.focus()
  const { unmount } = render(
    <StrictMode>
      <FluentProvider theme={webLightTheme}>
        <DetailPanel title="Access to model" onClose={vi.fn()}>Grant details</DetailPanel>
      </FluentProvider>
    </StrictMode>,
  )
  const close = screen.getByRole('button', { name: 'Close details' })
  close.focus()
  const restore = vi.spyOn(opener, 'focus')

  flushFrames()
  expect(restore).not.toHaveBeenCalled()
  expect(document.activeElement).toBe(close)

  unmount()
  flushFrames()
  expect(restore).toHaveBeenCalled()
  expect(document.activeElement).toBe(opener)
})
