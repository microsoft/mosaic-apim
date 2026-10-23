import { DataUsageRegular, AppsListRegular } from '@fluentui/react-icons'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { SectionLayout } from './SectionLayout'

const sections = [
  { value: 'overview', label: 'Overview & trends', icon: <DataUsageRegular /> },
  { value: 'resources', label: 'Resources & limits', icon: <AppsListRegular /> },
]

describe('SectionLayout', () => {
  beforeEach(() => localStorage.clear())

  it('keeps meaningful icons, accessible labels, and selection when collapsed', async () => {
    const user = userEvent.setup()
    const onSelect = vi.fn()
    render(
      <SectionLayout label="Usage" sections={sections} active="overview" onSelect={onSelect}>
        <p>Usage report</p>
      </SectionLayout>,
    )
    const navigation = within(screen.getByRole('navigation', { name: 'Usage sections' }))
    await user.click(navigation.getByRole('button', { name: 'Collapse Usage sections' }))
    expect(localStorage.getItem('mosaic.sections.Usage')).toBe('collapsed')
    expect(navigation.getByRole('button', { name: 'Expand Usage sections' })).toHaveAttribute('aria-expanded', 'false')
    expect(navigation.getByRole('button', { name: 'Overview & trends' })).toHaveAttribute('aria-current', 'page')
    const resources = navigation.getByRole('button', { name: 'Resources & limits' })
    expect(resources.querySelector('svg')).not.toBeNull()
    await user.click(resources)
    expect(onSelect).toHaveBeenCalledWith('resources')
    expect(screen.getByText('Usage report')).toBeVisible()
    await user.click(navigation.getByRole('button', { name: 'Expand Usage sections' }))
    expect(localStorage.getItem('mosaic.sections.Usage')).toBe('expanded')
  })

  it('restores collapsed navigation and offers the same sections in the compact selector', async () => {
    localStorage.setItem('mosaic.sections.Usage', 'collapsed')
    const user = userEvent.setup()
    const onSelect = vi.fn()
    render(
      <SectionLayout label="Usage" sections={sections} active="overview" onSelect={onSelect}>
        <p>Usage report</p>
      </SectionLayout>,
    )
    expect(screen.getByRole('button', { name: 'Expand Usage sections' })).toHaveAttribute('aria-expanded', 'false')
    await user.selectOptions(screen.getByRole('combobox', { name: 'Usage section' }), 'resources')
    expect(onSelect).toHaveBeenCalledWith('resources')
  })

  it('reserves active label width on expanded section labels', () => {
    render(
      <SectionLayout label="Usage" sections={sections} active="overview" onSelect={() => undefined}>
        <p>Usage report</p>
      </SectionLayout>,
    )
    const button = screen.getByRole('button', { name: 'Resources & limits' })
    expect(button.querySelector('[data-label="Resources & limits"]')).not.toBeNull()
  })
})
