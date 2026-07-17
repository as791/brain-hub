import { useEffect, useMemo, useRef } from 'react'
import ForceGraph3D from 'react-force-graph-3d'
import type { BrainEdge, BrainNode, GraphSnapshot } from '../types'
import { CONFIDENCE_COLORS, endpointId, escapeHtml, KIND_COLORS } from '../lib/graph'

interface GraphCamera {
  cameraPosition: (
    position: { x: number; y: number; z: number },
    lookAt?: { x: number; y: number; z: number },
    transitionMs?: number,
  ) => void
  zoomToFit: (transitionMs?: number, padding?: number) => void
}

interface Graph3DProps {
  graph: GraphSnapshot
  width: number
  height: number
  anchorId: string
  selectedId?: string
  pathEdgeIds: Set<string>
  reducedMotion: boolean
  onSelect: (node: BrainNode) => void
  onBackgroundClick: () => void
}

function nodeTooltip(node: BrainNode): string {
  return `<div class="graph-tooltip"><strong>${escapeHtml(node.label)}</strong><span>${escapeHtml(node.kind)} · ${Math.round(node.confidence * 100)}% confidence</span><p>${escapeHtml(node.summary)}</p></div>`
}

function edgeTooltip(edge: BrainEdge): string {
  return `<div class="graph-tooltip"><strong>${escapeHtml(edge.relation)}</strong><span>${escapeHtml(edge.confidenceClass)} · ${Math.round(edge.confidence * 100)}%</span><p>${escapeHtml(edge.explanation)}</p></div>`
}

export function Graph3D({
  graph,
  width,
  height,
  anchorId,
  selectedId,
  pathEdgeIds,
  reducedMotion,
  onSelect,
  onBackgroundClick,
}: Graph3DProps) {
  const graphRef = useRef<GraphCamera | null>(null)
  const nodeById = useMemo(() => new Map(graph.nodes.map((node) => [node.id, node])), [graph.nodes])

  useEffect(() => {
    if (!selectedId) return
    const node = nodeById.get(selectedId)
    if (!node || node.x === undefined || node.y === undefined || node.z === undefined) return
    const length = Math.hypot(node.x, node.y, node.z) || 1
    const distance = 82
    const ratio = 1 + distance / length
    graphRef.current?.cameraPosition(
      { x: node.x * ratio, y: node.y * ratio, z: node.z * ratio },
      { x: node.x, y: node.y, z: node.z },
      reducedMotion ? 0 : 800,
    )
  }, [nodeById, reducedMotion, selectedId])

  return (
    <ForceGraph3D
      ref={graphRef as never}
      width={width}
      height={height}
      graphData={{ nodes: graph.nodes, links: graph.edges }}
      nodeId="id"
      nodeLabel={(rawNode) => nodeTooltip(rawNode as BrainNode)}
      nodeColor={(rawNode) => {
        const node = rawNode as BrainNode
        return node.id === selectedId ? '#ffffff' : KIND_COLORS[node.kind]
      }}
      nodeVal={(rawNode) => {
        const node = rawNode as BrainNode
        return node.id === anchorId ? 10 : node.id === selectedId ? 8 : 4 + node.confidence * 3
      }}
      nodeOpacity={0.92}
      linkLabel={(rawLink) => edgeTooltip(rawLink as BrainEdge)}
      linkColor={(rawLink) => {
        const edge = rawLink as BrainEdge
        return pathEdgeIds.has(edge.id) ? '#ffffff' : CONFIDENCE_COLORS[edge.confidenceClass]
      }}
      linkWidth={(rawLink) => pathEdgeIds.has((rawLink as BrainEdge).id) ? 2.8 : 0.5 + (rawLink as BrainEdge).confidence}
      linkOpacity={0.48}
      linkDirectionalArrowLength={3.2}
      linkDirectionalArrowRelPos={0.82}
      linkCurvature={(rawLink) => {
        const edge = rawLink as BrainEdge
        const source = endpointId(edge.source)
        const target = endpointId(edge.target)
        const parallel = graph.edges.filter(
          (candidate) => endpointId(candidate.source) === source && endpointId(candidate.target) === target,
        )
        return parallel.length > 1 ? (parallel.findIndex((candidate) => candidate.id === edge.id) + 1) * 0.08 : 0
      }}
      linkDirectionalParticles={(rawLink) => pathEdgeIds.has((rawLink as BrainEdge).id) && !reducedMotion ? 3 : 0}
      linkDirectionalParticleWidth={2.4}
      linkDirectionalParticleSpeed={0.006}
      backgroundColor="rgba(0,0,0,0)"
      showNavInfo={false}
      enableNodeDrag={!reducedMotion}
      cooldownTicks={reducedMotion ? 1 : 130}
      d3AlphaDecay={0.03}
      d3VelocityDecay={0.34}
      onNodeClick={(rawNode) => onSelect(rawNode as BrainNode)}
      onBackgroundClick={onBackgroundClick}
    />
  )
}
