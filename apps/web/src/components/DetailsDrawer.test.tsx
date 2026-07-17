import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { demoGraph } from '../demoGraph'
import { DetailsDrawer } from './DetailsDrawer'

describe('node details accessibility', () => {
  it('implements keyboard-operable tabs with named tab panels', () => {
    render(
      <DetailsDrawer
        node={demoGraph.nodes[0]}
        graph={demoGraph}
        anchorId="another-node"
        path={null}
        pathLoading={false}
        onClose={vi.fn()}
        onMakeAnchor={vi.fn()}
        onExplainPath={vi.fn()}
        onSelect={vi.fn()}
      />,
    )

    const contextTab = screen.getByRole('tab', { name: 'Context' })
    const evidenceTab = screen.getByRole('tab', { name: /Evidence/ })
    expect(contextTab).toHaveAttribute('aria-selected', 'true')

    fireEvent.keyDown(contextTab, { key: 'ArrowRight' })

    expect(evidenceTab).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('tabpanel')).toHaveAttribute(
      'aria-labelledby',
      'node-evidence-tab',
    )
  })
})
