import { useEffect, useRef } from 'react'
import ForceGraph2D from 'react-force-graph-2d'
import type { BrainEdge, BrainNode, GraphSnapshot } from '../types'
import { CONFIDENCE_COLORS, KIND_COLORS } from '../lib/graph'

interface Graph2DRef {
  centerAt: (x?: number, y?: number, transitionMs?: number) => void
  zoom: (scale?: number, transitionMs?: number) => void
  zoomToFit: (transitionMs?: number, padding?: number) => void
}

interface Graph2DProps {
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

export function Graph2D({
  graph,
  width,
  height,
  anchorId,
  selectedId,
  pathEdgeIds,
  reducedMotion,
  onSelect,
  onBackgroundClick,
}: Graph2DProps) {
  const graphRef = useRef<Graph2DRef | null>(null)

  useEffect(() => {
    if (!selectedId) return
    const node = graph.nodes.find((candidate) => candidate.id === selectedId)
    if (!node || node.x === undefined || node.y === undefined) return
    graphRef.current?.centerAt(node.x, node.y, reducedMotion ? 0 : 550)
    graphRef.current?.zoom(3.2, reducedMotion ? 0 : 550)
  }, [graph.nodes, reducedMotion, selectedId])

  return (
    <ForceGraph2D
      ref={graphRef as never}
      width={width}
      height={height}
      graphData={{ nodes: graph.nodes, links: graph.edges }}
      nodeId="id"
      dagMode="td"
      dagLevelDistance={90}
      nodeLabel={(rawNode) => `${(rawNode as BrainNode).label} · ${(rawNode as BrainNode).kind}`}
      nodeCanvasObject={(rawNode, context, globalScale) => {
        const node = rawNode as BrainNode
        const selected = node.id === selectedId
        const anchor = node.id === anchorId
        const radius = anchor ? 7 : selected ? 6 : 4
        context.beginPath()
        context.arc(node.x ?? 0, node.y ?? 0, radius, 0, 2 * Math.PI)
        context.fillStyle = selected ? '#ffffff' : KIND_COLORS[node.kind]
        context.shadowColor = KIND_COLORS[node.kind]
        context.shadowBlur = selected || anchor ? 16 : 6
        context.fill()
        context.shadowBlur = 0
        if (anchor || selected || globalScale > 2.2) {
          const fontSize = Math.max(3.2, 12 / globalScale)
          context.font = `600 ${fontSize}px Inter, system-ui, sans-serif`
          context.textAlign = 'center'
          context.textBaseline = 'top'
          context.fillStyle = '#eafaff'
          context.fillText(node.label, node.x ?? 0, (node.y ?? 0) + radius + 2)
        }
      }}
      nodePointerAreaPaint={(rawNode, color, context) => {
        const node = rawNode as BrainNode
        context.beginPath()
        context.arc(node.x ?? 0, node.y ?? 0, 9, 0, 2 * Math.PI)
        context.fillStyle = color
        context.fill()
      }}
      linkColor={(rawLink) => {
        const edge = rawLink as BrainEdge
        return pathEdgeIds.has(edge.id) ? '#ffffff' : CONFIDENCE_COLORS[edge.confidenceClass]
      }}
      linkWidth={(rawLink) => pathEdgeIds.has((rawLink as BrainEdge).id) ? 3 : 0.8}
      linkDirectionalArrowLength={4}
      linkDirectionalArrowRelPos={0.86}
      backgroundColor="rgba(0,0,0,0)"
      enableNodeDrag={!reducedMotion}
      cooldownTicks={reducedMotion ? 1 : 110}
      onNodeClick={(rawNode) => onSelect(rawNode as BrainNode)}
      onBackgroundClick={onBackgroundClick}
    />
  )
}
