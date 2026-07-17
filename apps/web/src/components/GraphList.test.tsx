import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { demoGraph } from '../demoGraph'
import { GraphList } from './GraphList'

describe('accessible graph list', () => {
  it('exposes every node as a keyboard-operable button', () => {
    const onSelect = vi.fn()
    render(<GraphList graph={demoGraph} anchorId="ws-brain" onSelect={onSelect} />)

    const node = screen.getByRole('button', { name: /Brain Hub product/i })
    expect(node).toBeVisible()
    fireEvent.click(node)
    expect(onSelect).toHaveBeenCalledWith(expect.objectContaining({ id: 'ws-brain' }))
  })
})
