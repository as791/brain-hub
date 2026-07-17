import type { BrainEdge, BrainNode, GraphSnapshot } from '../types'
import { endpointId, KIND_COLORS } from '../lib/graph'

interface GraphListProps {
  graph: GraphSnapshot
  anchorId: string
  selectedId?: string
  onSelect: (node: BrainNode) => void
}

export function GraphList({ graph, anchorId, selectedId, onSelect }: GraphListProps) {
  const connections = (nodeId: string): BrainEdge[] =>
    graph.edges.filter((edge) => endpointId(edge.source) === nodeId || endpointId(edge.target) === nodeId)

  return (
    <div className="graph-list" role="region" aria-label="Knowledge graph as an accessible list">
      <div className="graph-list__intro">
        <span>Keyboard view</span>
        <p>Each node includes its visible connections at the selected time.</p>
      </div>
      <ol>
        {graph.nodes.map((node) => {
          const linked = connections(node.id)
          return (
            <li key={node.id} className={node.id === selectedId ? 'is-selected' : undefined}>
              <button type="button" onClick={() => onSelect(node)} aria-current={node.id === selectedId ? 'true' : undefined}>
                <span className="node-dot" style={{ '--node-color': KIND_COLORS[node.kind] } as React.CSSProperties} />
                <span className="graph-list__label">
                  <strong>{node.label}</strong>
                  <small>{node.kind}{node.id === anchorId ? ' · current anchor' : ''}</small>
                </span>
                <span className="graph-list__count">{linked.length}</span>
              </button>
              <div className="sr-only">
                {linked.map((edge) => `${edge.relation}: ${edge.explanation}`).join('. ')}
              </div>
            </li>
          )
        })}
      </ol>
    </div>
  )
}
