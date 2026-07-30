import type { BrainNode, ConnectionMode, GraphSnapshot } from '../types'
import { displayTitle, formatLocalTime, safeDisplayText } from '../lib/display'
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

const laneOrder: Lane[] = ['attention', 'active', 'waiting', 'unknown', 'done']
const eventKinds = new Set(['Run', 'Task', 'Decision', 'Artifact'])

function eventTime(node: BrainNode): number {
  const time = Date.parse(node.validFrom || node.recordedAt)
  return Number.isNaN(time) ? 0 : time
}

function capturedCount(graph: GraphSnapshot, keys: string[]): number {
  return graph.nodes.filter((node) => keys.some((key) => safeDisplayText(node.metadata?.[key], '') !== '')).length
}

export function CommandCenter({ graph, connection, onOpenRelationships }: Props) {
  const { tasks, tests } = workStatus(graph)
  const workstreams = graph.nodes.filter((node) => node.kind === 'Workstream')
  const timeline = graph.nodes.filter((node) => eventKinds.has(node.kind)).sort((a, b) => eventTime(b) - eventTime(a)).slice(0, 10)
  const runs = graph.nodes.filter((node) => node.kind === 'Run').sort((a, b) => eventTime(b) - eventTime(a)).slice(0, 3)
  const completed = tasks.filter((task) => task.state === 'completed').length
  const nextAction = workstreams.map((node) => node.metadata?.nextAction ?? node.metadata?.next_action).find((value) => typeof value === 'string')
  const toolActivity = capturedCount(graph, ['toolName'])
  const mcpActivity = capturedCount(graph, ['mcpToolName', 'mcpServer', 'mcpServerName'])
  const groupedWorkstreams = Array.from(workstreams.reduce((groups, node) => {
    const lane = workstreamLane(node)
    const title = displayTitle(node, graph)
    const key = `${lane}:${title}`
    const current = groups.get(key)
    groups.set(key, current ? { ...current, count: current.count + 1 } : { node, lane, title, count: 1 })
    return groups
  }, new Map<string, { node: BrainNode; lane: Lane; title: string; count: number }>()).values())
    .sort((a, b) => laneOrder.indexOf(a.lane) - laneOrder.indexOf(b.lane))
    .slice(0, 8)

  return (
    <main id="main-content" className="command-center">
      <header className="command-center__intro">
        <div>
          <span className="command-eyebrow">Local work memory</span>
          <h1>Command Center</h1>
          <p>Current work, recent activity, and capture availability at a glance.</p>
        </div>
        <div className="command-summary">
          <span>Next action</span>
          <strong>{safeDisplayText(nextAction, 'Not captured yet')}</strong>
          <small>{tasks.length ? `${completed} of ${tasks.length} tasks complete` : 'Task progress not captured yet'}</small>
        </div>
      </header>

      <section className="usage-strip" aria-label="Activity and capture summary">
        <div><span>Workstreams</span><strong>{workstreams.length}</strong></div>
        <div><span>Recent activity</span><strong>{timeline.length}</strong></div>
        <div><span>Tool activity</span><strong>{toolActivity ? `${toolActivity} captured` : 'Not captured yet'}</strong></div>
        <div><span>MCP tools</span><strong>{mcpActivity ? `${mcpActivity} captured` : 'Not captured yet'}</strong></div>
      </section>

      <section className="command-grid">
        <div className="command-primary">
          <section className="workstream-board" aria-labelledby="workstreams-title">
            <header><div><span className="command-eyebrow">Workstreams</span><h2 id="workstreams-title">What needs your attention</h2></div><small>{workstreams.length} total</small></header>
            {groupedWorkstreams.length ? <div className="workstream-list">
              {groupedWorkstreams.map(({ node, lane, title, count }) => <button key={`${lane}:${title}`} type="button" onClick={() => onOpenRelationships(node)}>
                <span className="workstream-state" data-lane={lane}>{laneCopy[lane]}</span>
                <span><strong>{title}</strong><small>{safeDisplayText(node.summary, 'Summary not captured yet')}</small></span>
                <em>{count > 1 ? `${count} records` : 'View relationships'} →</em>
              </button>)}
            </div> : <p className="command-empty">Workstream activity is not captured yet.</p>}
          </section>

          <section className="activity-timeline" aria-labelledby="timeline-title">
            <header><div><span className="command-eyebrow">Readable timeline</span><h2 id="timeline-title">Recent activity</h2></div><small>{timeline.length} events shown</small></header>
            {timeline.length ? <ol>{timeline.map((node) => <li key={node.id}>
              <time dateTime={node.validTimeKnown === false ? undefined : node.validFrom}>{formatLocalTime(node)}</time>
              <span className="timeline-marker" data-kind={node.kind} />
              <div><small>{node.kind} · {safeDisplayText(node.provenance[0]?.agent, 'Source not captured')}</small><strong>{displayTitle(node, graph)}</strong><p>{safeDisplayText(node.summary, 'Details not captured yet')}</p></div>
            </li>)}</ol> : <p className="command-empty">Recent activity is not captured yet.</p>}
          </section>
        </div>

        <aside className="command-secondary">
          <section><header><div><span className="command-eyebrow">Recent activity</span><h2>Latest captured runs</h2></div></header>
            {runs.length ? <ul>{runs.map((run) => <li key={run.id}><strong>{displayTitle(run, graph)}</strong><span>{safeDisplayText(run.provenance[0]?.agent, 'Source not captured')} · {formatLocalTime(run)}</span></li>)}</ul> : <p className="command-empty">Run activity is not captured yet.</p>}
          </section>
          <section><header><div><span className="command-eyebrow">Capture status</span><h2>Tools &amp; verification</h2></div></header>
            <dl><div><dt>Tool activity</dt><dd>{toolActivity ? `${toolActivity} captured` : 'Not captured yet'}</dd></div><div><dt>MCP tools</dt><dd>{mcpActivity ? `${mcpActivity} captured` : 'Not captured yet'}</dd></div><div><dt>Verification</dt><dd>{tests.length ? `${tests.length} result${tests.length === 1 ? '' : 's'} captured` : 'Not captured yet'}</dd></div><div><dt>Daemon</dt><dd>{connection === 'live' ? 'Connected' : connection === 'demo' ? 'Demo data' : 'Offline'}</dd></div></dl>
            <p className="privacy-note">Only allowlisted graph fields are shown. Tool payloads and private content stay out of this view.</p>
          </section>
        </aside>
      </section>
    </main>
  )
}
