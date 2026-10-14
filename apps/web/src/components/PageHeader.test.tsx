import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { PageHeader, PreviewNotice } from './PageHeader'

describe('PageHeader', () => {
  it('keeps the heading and description without redundant live-data decoration', () => {
    render(<PageHeader title="Models" description="Manage model publication." source="live" />)
    expect(screen.getByRole('heading', { name: 'Models' })).toBeVisible()
    expect(screen.getByText('Manage model publication.')).toBeVisible()
    expect(screen.queryByText('Live data')).not.toBeInTheDocument()
  })
  it('identifies sample-backed experiences', () => {
    render(
      <>
        <PageHeader
          title="Analytics"
          description="Operational insights"
          source="sample"
        />
        <PreviewNotice>Azure Monitor is not queried.</PreviewNotice>
      </>,
    )

    expect(screen.getByRole('heading', { name: 'Analytics' })).toBeVisible()
    expect(screen.getAllByText('Sample data')).toHaveLength(2)
    expect(screen.getByRole('note')).toHaveTextContent('Azure Monitor is not queried.')
  })
})
