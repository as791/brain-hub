import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { CommandCenter } from './CommandCenter'
import type { BrainNode, GraphSnapshot } from '../types'

const node = (id: string, kind: BrainNode['kind'], metadata: Record<string, unknown> = {}): BrainNode => ({
  id, kind, metadata, label: `${id} title`, summary: `${id} summary`, validFrom: '2026-07-30T10:00:00Z', recordedAt: '2026-07-30T10:00:00Z',
  confidence: 1, confidenceClass: 'EXTRACTED', provenance: [{ actor: 'test', agent: 'Codex' }], evidence: [], tags: [],
})

afterEach(cleanup)

describe('Command Center', () => {
  it('prioritizes work and timeline while keeping missing telemetry explicit', () => {
    const graph: GraphSnapshot = { nodes: [node('work', 'Workstream', { status: 'active' }), node('run', 'Run')], edges: [] }
    render(<CommandCenter graph={graph} connection="live" onOpenRelationships={vi.fn()} />)

    expect(screen.getByRole('heading', { name: 'What needs your attention' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Recent activity' })).toBeInTheDocument()
    expect(screen.getAllByText('Unavailable').length).toBeGreaterThanOrEqual(6)
    expect(screen.queryByLabelText('Brain Hub graph explorer')).not.toBeInTheDocument()
  })

  it('does not turn a missing workstream state into a known state', () => {
    const view = render(<CommandCenter graph={{ nodes: [node('work', 'Workstream')], edges: [] }} connection="demo" onOpenRelationships={vi.fn()} />)

    expect(view.getByRole('region', { name: 'State unavailable' })).toHaveTextContent('No workstream state was provided')
  })
})
