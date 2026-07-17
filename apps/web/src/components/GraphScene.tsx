import { lazy, Suspense, useMemo, useRef } from 'react'
import type { BrainNode, GraphSnapshot, SceneMode } from '../types'
import { cloneForRenderer } from '../lib/graph'
import { useElementSize } from '../hooks/useElementSize'
import { GraphList } from './GraphList'

const Graph2D = lazy(() => import('./Graph2D').then((module) => ({ default: module.Graph2D })))
const Graph3D = lazy(() => import('./Graph3D').then((module) => ({ default: module.Graph3D })))

interface GraphSceneProps {
  graph: GraphSnapshot
  mode: SceneMode
  anchorId: string
  selectedId?: string
  pathEdgeIds: Set<string>
  reducedMotion: boolean
  onSelect: (node: BrainNode) => void
  onBackgroundClick: () => void
}

export function GraphScene({
  graph,
  mode,
  anchorId,
  selectedId,
  pathEdgeIds,
  reducedMotion,
  onSelect,
  onBackgroundClick,
}: GraphSceneProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const { width, height } = useElementSize(containerRef)
  const rendererGraph = useMemo(() => cloneForRenderer(graph), [graph])

  return (
    <div
      className="graph-stage"
      ref={containerRef}
      data-scene-mode={mode}
      aria-hidden={mode === 'list' ? undefined : true}
    >
      {mode === 'list' ? (
        <GraphList graph={rendererGraph} anchorId={anchorId} selectedId={selectedId} onSelect={onSelect} />
      ) : (
        <Suspense fallback={<div className="scene-loading">Preparing graph scene…</div>}>
          {mode === '3d' ? (
            <Graph3D
              graph={rendererGraph}
              width={width}
              height={height}
              anchorId={anchorId}
              selectedId={selectedId}
              pathEdgeIds={pathEdgeIds}
              reducedMotion={reducedMotion}
              onSelect={onSelect}
              onBackgroundClick={onBackgroundClick}
            />
          ) : (
            <Graph2D
              graph={rendererGraph}
              width={width}
              height={height}
              anchorId={anchorId}
              selectedId={selectedId}
              pathEdgeIds={pathEdgeIds}
              reducedMotion={reducedMotion}
              onSelect={onSelect}
              onBackgroundClick={onBackgroundClick}
            />
          )}
        </Suspense>
      )}
      <div className="scene-vignette" aria-hidden="true" />
    </div>
  )
}
