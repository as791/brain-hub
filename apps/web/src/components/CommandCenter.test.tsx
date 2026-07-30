import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { CommandCenter } from './CommandCenter'
import { formatLocalTime } from '../lib/display'
import type { BrainNode, GraphSnapshot } from '../types'

const node = (id: string, kind: BrainNode['kind'], metadata: Record<string, unknown> = {}): BrainNode => ({
  id, kind, metadata, label: `${id} title`, summary: `${id} summary`, validFrom: '2026-07-30T10:00:00Z', recordedAt: '2026-07-30T10:00:00Z',
  confidence: 1, confidenceClass: 'EXTRACTED', provenance: [{ actor: 'test', agent: 'Codex' }], evidence: [], tags: [],
})

afterEach(cleanup)

describe('Command Center', () => {
  it('uses safe display names and browser-local timestamps without exposing identifiers', () => {
    const uuid = 'f47ac10b-58cc-4372-a567-0e02b2c3d479'
    const graph: GraphSnapshot = {
      nodes: [
        { ...node('work', 'Workstream', { status: 'active', telemetryName: 'Codex telemetry' }), label: `sessions/session_${uuid}`, summary: `Captured ${uuid}` },
        { ...node('run', 'Run', { titleSource: 'generated-safe-metadata' }), label: `codex · ${uuid} · completed run`, validTimeKnown: true },
      ],
      edges: [],
    }
    render(<CommandCenter graph={graph} connection="live" onOpenRelationships={vi.fn()} />)

    expect(screen.getByText('Codex telemetry')).toBeInTheDocument()
    expect(screen.getByRole('region', { name: 'Recent activity' })).toHaveTextContent('Codex activity')
    expect(screen.getAllByText(formatLocalTime(graph.nodes[1])).length).toBeGreaterThan(0)
    expect(screen.queryByText(/UTC/)).not.toBeInTheDocument()
    expect(document.body).not.toHaveTextContent(uuid)
    expect(screen.queryByLabelText('Brain Hub graph explorer')).not.toBeInTheDocument()
  })

  it('does not turn a missing workstream state into a known state', () => {
    const view = render(<CommandCenter graph={{ nodes: [node('work', 'Workstream')], edges: [] }} connection="demo" onOpenRelationships={vi.fn()} />)

    expect(view.getByText('State unavailable')).toBeInTheDocument()
  })

  it('uses availability language when activity was not captured', () => {
    render(<CommandCenter graph={{ nodes: [], edges: [] }} connection="live" onOpenRelationships={vi.fn()} />)

    expect(screen.getAllByText('Not captured yet').length).toBeGreaterThanOrEqual(6)
    expect(screen.getByText('Run activity is not captured yet.')).toBeInTheDocument()
    expect(document.body).not.toHaveTextContent(/No runs|Unavailable/)
  })
})
