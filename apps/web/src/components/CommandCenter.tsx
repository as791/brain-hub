import type { BrainNode, ConnectionMode, GraphSnapshot } from '../types'
import { workStatus } from '../lib/workStatus'

interface Props {
  graph: GraphSnapshot
  connection: ConnectionMode
  onOpenRelationships: (node: BrainNode) => void
}

type Lane = 'active' | 'waiting' | 'attention' | 'done' | 'unknown'

const laneCopy: Record<Lane, string> = {
  active: 'Active',
  waiting: 'Waiting',
  attention: 'Needs attention',
  done: 'Done',
  unknown: 'State unavailable',
}

function workstreamLane(node: BrainNode): Lane {
  const state = String(node.metadata?.state ?? node.metadata?.status ?? '').toLowerCase().replaceAll('_', '-')
  if (state === 'active' || state === 'in-progress') return 'active'
  if (state === 'waiting') return 'waiting'
  if (state === 'blocked' || state === 'needs-attention' || state === 'failed') return 'attention'
  if (state === 'done' || state === 'completed') return 'done'
  return 'unknown'
}

const eventKinds = new Set(['Run', 'Task', 'Decision', 'Artifact'])

function eventTime(node: BrainNode): number {
  const time = Date.parse(node.validFrom || node.recordedAt)
  return Number.isNaN(time) ? 0 : time
}

function readableTime(node: BrainNode): string {
  const time = eventTime(node)
  return time ? new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(time) : 'Time unavailable'
}

export function CommandCenter({ graph, connection, onOpenRelationships }: Props) {
  const { tasks, tests } = workStatus(graph)
  const workstreams = graph.nodes.filter((node) => node.kind === 'Workstream')
  const timeline = graph.nodes.filter((node) => eventKinds.has(node.kind)).sort((a, b) => eventTime(b) - eventTime(a)).slice(0, 10)
  const runs = graph.nodes.filter((node) => node.kind === 'Run').sort((a, b) => eventTime(b) - eventTime(a)).slice(0, 3)
  const completed = tasks.filter((task) => task.state === 'completed').length
  const nextAction = workstreams.map((node) => node.metadata?.nextAction ?? node.metadata?.next_action).find((value) => typeof value === 'string')

  return (
    <main id="main-content" className="command-center">
      <header className="command-center__intro">
        <div>
          <span className="command-eyebrow">Today · local work memory</span>
          <h1>Command Center</h1>
          <p>Follow active work, handoffs, and verification without opening the relationship graph.</p>
        </div>
        <div className="command-summary">
          <span>Next action</span>
          <strong>{typeof nextAction === 'string' ? nextAction : 'Not recorded'}</strong>
          <small>{tasks.length ? `${completed} of ${tasks.length} tasks complete` : 'Progress unavailable'}</small>
        </div>
      </header>

      <section className="usage-strip" aria-label="Usage and capability summary">
        {['Tokens', 'Cache reuse', 'Tools', 'MCP servers'].map((label) => <div key={label}><span>{label}</span><strong>Unavailable</strong></div>)}
        <p>Telemetry is not available from the current API.</p>
      </section>

      <section className="command-grid">
        <div className="command-primary">
          <section className="workstream-board" aria-labelledby="workstreams-title">
            <header><div><span className="command-eyebrow">Workstreams</span><h2 id="workstreams-title">What needs your attention</h2></div><small>{workstreams.length} total</small></header>
            <div className="workstream-lanes">
              {(['active', 'waiting', 'attention', 'done', 'unknown'] as Lane[]).map((lane) => {
                const items = workstreams.filter((node) => workstreamLane(node) === lane)
                return <section key={lane} className="workstream-lane" data-lane={lane} aria-label={laneCopy[lane]}>
                  <header><strong>{laneCopy[lane]}</strong><span>{items.length}</span></header>
                  {items.map((node) => <button key={node.id} type="button" onClick={() => onOpenRelationships(node)}>
                    <strong>{node.label}</strong>
                    <span>{node.summary || 'Summary unavailable'}</span>
                    <small>{workstreamLane(node) === 'unknown' ? 'No workstream state was provided' : 'Open relationships →'}</small>
                  </button>)}
                  {!items.length && <p>No workstreams</p>}
                </section>
              })}
            </div>
          </section>

          <section className="activity-timeline" aria-labelledby="timeline-title">
            <header><div><span className="command-eyebrow">Readable timeline</span><h2 id="timeline-title">Recent activity</h2></div><small>{timeline.length} events shown</small></header>
            {timeline.length ? <ol>{timeline.map((node) => <li key={node.id}>
              <time dateTime={node.validFrom}>{readableTime(node)}</time>
              <span className="timeline-marker" data-kind={node.kind} />
              <div><small>{node.kind} · {node.provenance[0]?.agent ?? 'Source unavailable'}</small><strong>{node.label}</strong><p>{node.summary || 'Details unavailable'}</p></div>
            </li>)}</ol> : <p className="command-empty">No timeline activity is available.</p>}
          </section>
        </div>

        <aside className="command-secondary">
          <section><header><div><span className="command-eyebrow">Recent runs</span><h2>Latest sessions</h2></div></header>
            {runs.length ? <ul>{runs.map((run) => <li key={run.id}><strong>{run.label}</strong><span>{run.provenance[0]?.agent ?? 'Source unavailable'} · {readableTime(run)}</span></li>)}</ul> : <p className="command-empty">No runs recorded.</p>}
          </section>
          <section><header><div><span className="command-eyebrow">Tool &amp; MCP health</span><h2>Capability status</h2></div></header>
            <dl><div><dt>Tool health</dt><dd>Unavailable</dd></div><div><dt>MCP health</dt><dd>Unavailable</dd></div><div><dt>Verification</dt><dd>{tests.length ? `${tests.length} result${tests.length === 1 ? '' : 's'}` : 'Unavailable'}</dd></div><div><dt>Daemon</dt><dd>{connection === 'live' ? 'Connected' : connection === 'demo' ? 'Demo data' : 'Offline'}</dd></div></dl>
            <p className="privacy-note">Only allowlisted graph fields are shown. Tool payloads and private content stay out of this view.</p>
          </section>
        </aside>
      </section>
    </main>
  )
}
