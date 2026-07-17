import { useRef, useState, type KeyboardEvent } from 'react'
import type { BrainEdge, BrainNode, GraphSnapshot, PathResponse } from '../types'
import { CONFIDENCE_COLORS, endpointId, formatMoment, KIND_COLORS } from '../lib/graph'

interface DetailsDrawerProps {
  node?: BrainNode
  graph: GraphSnapshot
  anchorId: string
  path?: PathResponse | null
  pathLoading: boolean
  onClose: () => void
  onMakeAnchor: (node: BrainNode) => void
  onExplainPath: (node: BrainNode) => void
  onSelect: (node: BrainNode) => void
}

function EvidenceLink({ uri, label }: { uri?: string; label: string }) {
  if (uri?.startsWith('http://') || uri?.startsWith('https://')) {
    return <a href={uri} target="_blank" rel="noreferrer">{label}<span aria-hidden="true"> ↗</span></a>
  }
  return <span>{label}</span>
}

function EdgeItem({ edge, nodeId, nodes, onSelect }: {
  edge: BrainEdge
  nodeId: string
  nodes: Map<string, BrainNode>
  onSelect: (node: BrainNode) => void
}) {
  const sourceId = endpointId(edge.source)
  const otherId = sourceId === nodeId ? endpointId(edge.target) : sourceId
  const other = nodes.get(otherId)
  if (!other) return null
  const outgoing = sourceId === nodeId
  return (
    <li>
      <button type="button" onClick={() => onSelect(other)}>
        <span className="edge-direction" aria-label={outgoing ? 'outgoing edge' : 'incoming edge'}>{outgoing ? '→' : '←'}</span>
        <span>
          <small>{edge.relation.replaceAll('_', ' ')}</small>
          <strong>{other.label}</strong>
          <em>{edge.explanation}</em>
        </span>
        <span className="edge-confidence">{Math.round(edge.confidence * 100)}%</span>
      </button>
    </li>
  )
}

export function DetailsDrawer({
  node,
  graph,
  anchorId,
  path,
  pathLoading,
  onClose,
  onMakeAnchor,
  onExplainPath,
  onSelect,
}: DetailsDrawerProps) {
  const [tab, setTab] = useState<'context' | 'evidence'>('context')
  const contextTabRef = useRef<HTMLButtonElement>(null)
  const evidenceTabRef = useRef<HTMLButtonElement>(null)
  if (!node) return null
  const nodes = new Map(graph.nodes.map((candidate) => [candidate.id, candidate]))
  const edges = graph.edges.filter(
    (edge) => endpointId(edge.source) === node.id || endpointId(edge.target) === node.id,
  )
  const isAnchor = node.id === anchorId

  const moveTab = (next: 'context' | 'evidence') => {
    setTab(next)
    const target = next === 'context' ? contextTabRef : evidenceTabRef
    window.requestAnimationFrame(() => target.current?.focus())
  }

  const onTabKeyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return
    event.preventDefault()
    moveTab(tab === 'context' ? 'evidence' : 'context')
  }

  return (
    <aside className="details-drawer" aria-label={`Details for ${node.label}`}>
      <header className="details-drawer__header">
        <div className="node-kind" style={{ '--kind-color': KIND_COLORS[node.kind] } as React.CSSProperties}>
          <span /> {node.kind}
        </div>
        <button type="button" className="icon-button" onClick={onClose} aria-label="Close node details">×</button>
      </header>
      <div className="details-drawer__title">
        <h2>{node.label}</h2>
        <div className="confidence-pill" style={{ '--confidence-color': CONFIDENCE_COLORS[node.confidenceClass] } as React.CSSProperties}>
          <span /> {node.confidenceClass.toLocaleLowerCase()} · {Math.round(node.confidence * 100)}%
        </div>
        <p>{node.summary}</p>
      </div>

      <div className="drawer-actions">
        <button type="button" className="primary-button" onClick={() => onMakeAnchor(node)} disabled={isAnchor}>
          {isAnchor ? 'Current search anchor' : 'Start searches here'}
        </button>
        {!isAnchor && (
          <button type="button" className="secondary-button" onClick={() => onExplainPath(node)} disabled={pathLoading}>
            {pathLoading ? 'Tracing…' : 'Explain path'}
          </button>
        )}
      </div>

      {path && path.targetId === node.id && (
        <section className="path-card" aria-label="Explained path">
          <div className="path-card__heading">
            <span className="eyebrow">Evidence path</span>
            <strong>{path.steps.length} step{path.steps.length === 1 ? '' : 's'} · {Math.round(path.confidence * 100)}% floor</strong>
          </div>
          <ol>
            {path.steps.map((step) => (
              <li key={step.edge.id}>
                <span>{step.from.label}</span>
                <small>{step.edge.relation.replaceAll('_', ' ')}</small>
                <span>{step.to.label}</span>
              </li>
            ))}
          </ol>
          <p>{path.explanation}</p>
        </section>
      )}

      <div className="drawer-tabs" role="tablist" aria-label="Node detail sections">
        <button
          ref={contextTabRef}
          id="node-context-tab"
          type="button"
          role="tab"
          aria-controls="node-context-panel"
          aria-selected={tab === 'context'}
          tabIndex={tab === 'context' ? 0 : -1}
          onClick={() => setTab('context')}
          onKeyDown={onTabKeyDown}
        >
          Context
        </button>
        <button
          ref={evidenceTabRef}
          id="node-evidence-tab"
          type="button"
          role="tab"
          aria-controls="node-evidence-panel"
          aria-selected={tab === 'evidence'}
          tabIndex={tab === 'evidence' ? 0 : -1}
          onClick={() => setTab('evidence')}
          onKeyDown={onTabKeyDown}
        >
          Evidence <span>{node.evidence.length}</span>
        </button>
      </div>

      {tab === 'context' ? (
        <div
          id="node-context-panel"
          className="drawer-section"
          role="tabpanel"
          aria-labelledby="node-context-tab"
          tabIndex={0}
        >
          <dl className="fact-grid">
            <div><dt>Valid from</dt><dd>{node.validTimeKnown === false ? 'Unknown' : formatMoment(node.validFrom)}</dd></div>
            <div><dt>Valid until</dt><dd>{node.validTo ? formatMoment(node.validTo) : 'Present'}</dd></div>
            <div><dt>Recorded</dt><dd>{formatMoment(node.recordedAt)}</dd></div>
            <div><dt>Review</dt><dd>{node.reviewState?.replace('-', ' ') ?? 'accepted'}</dd></div>
          </dl>
          <section className="connections-section">
            <div className="section-heading"><h3>Connections</h3><span>{edges.length}</span></div>
            {edges.length > 0 ? (
              <ul>{edges.map((edge) => <EdgeItem key={edge.id} edge={edge} nodeId={node.id} nodes={nodes} onSelect={onSelect} />)}</ul>
            ) : <p className="empty-copy">No visible connections at this time.</p>}
          </section>
          <section className="tags-section">
            <h3>Tags</h3>
            <div>{node.tags.map((tag) => <span key={tag}>{tag}</span>)}</div>
          </section>
        </div>
      ) : (
        <div
          id="node-evidence-panel"
          className="drawer-section evidence-section"
          role="tabpanel"
          aria-labelledby="node-evidence-tab"
          tabIndex={0}
        >
          <section>
            <div className="section-heading"><h3>Source evidence</h3><span>{node.evidence.length}</span></div>
            <ol>
              {node.evidence.map((evidence) => (
                <li key={evidence.id}>
                  <EvidenceLink uri={evidence.uri} label={evidence.label} />
                  {evidence.excerpt && <blockquote>{evidence.excerpt}</blockquote>}
                  {evidence.contentHash && <code>{evidence.contentHash}</code>}
                </li>
              ))}
            </ol>
          </section>
          <section>
            <div className="section-heading"><h3>Provenance</h3><span>{node.provenance.length}</span></div>
            <ol className="provenance-list">
              {node.provenance.map((provenance, index) => (
                <li key={`${provenance.actor}-${index}`}>
                  <strong>{provenance.agent ?? provenance.actor}</strong>
                  <span>{provenance.actor}</span>
                  <small>{provenance.extractor}{provenance.extractorVersion ? ` v${provenance.extractorVersion}` : ''}</small>
                </li>
              ))}
            </ol>
          </section>
        </div>
      )}
    </aside>
  )
}
