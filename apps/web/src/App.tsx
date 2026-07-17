import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { BrainHubApiError, brainHubApi, getApiToken, setRuntimeApiToken, subscribeToEvents } from './api'
import { demoGraph } from './demoGraph'
import { DetailsDrawer } from './components/DetailsDrawer'
import { Filters } from './components/Filters'
import { GraphScene } from './components/GraphScene'
import { Timeline } from './components/Timeline'
import { useReducedMotion, useWebGLSupport } from './hooks/useMedia'
import {
  applySceneBudget,
  boundedSubgraph,
  endpointId,
  filterAtTime,
  formatMoment,
  graphTimeBounds,
  KIND_COLORS,
  localSearch,
  MAX_GRAPH_HOPS,
  shortestPath,
  topDownPath,
  topDownSubgraph,
} from './lib/graph'
import type {
  BrainNode,
  ConfidenceClass,
  ConnectionMode,
  GraphSnapshot,
  NodeKind,
  PathResponse,
  SceneMode,
  SearchHit,
} from './types'

const EMPTY_GRAPH: GraphSnapshot = { nodes: [], edges: [] }
const SCENE_BUDGET = { maxNodes: 2_000, maxEdges: 10_000 }

function App() {
  const reducedMotion = useReducedMotion()
  const webGLSupported = useWebGLSupport()
  const searchRef = useRef<HTMLInputElement>(null)
  const [sourceGraph, setSourceGraph] = useState<GraphSnapshot>(demoGraph)
  const [searchGraph, setSearchGraph] = useState<GraphSnapshot | null>(null)
  const [connection, setConnection] = useState<ConnectionMode>('demo')
  const [connectionMessage, setConnectionMessage] = useState('Checking local daemon…')
  const [streamOpen, setStreamOpen] = useState(false)
  const [sceneMode, setSceneMode] = useState<SceneMode>(() =>
    window.matchMedia?.('(max-width: 720px)').matches ? '2d' : '3d',
  )
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [tokenDraft, setTokenDraft] = useState('')
  const [tokenRevision, setTokenRevision] = useState(0)
  const [anchorId, setAnchorId] = useState(demoGraph.anchorId ?? demoGraph.nodes[0].id)
  const [navigationPath, setNavigationPath] = useState<string[]>([
    demoGraph.anchorId ?? demoGraph.nodes[0].id,
  ])
  const [selectedId, setSelectedId] = useState<string>()
  const [query, setQuery] = useState('')
  const [hops, setHops] = useState(2)
  const [hits, setHits] = useState<SearchHit[]>([])
  const [searching, setSearching] = useState(false)
  const [hiddenKinds, setHiddenKinds] = useState<Set<NodeKind>>(new Set())
  const [hiddenConfidence, setHiddenConfidence] = useState<Set<ConfidenceClass>>(new Set())
  const [path, setPath] = useState<PathResponse | null>(null)
  const [pathLoading, setPathLoading] = useState(false)
  const [error, setError] = useState<string>()
  const initialBounds = graphTimeBounds(demoGraph)
  const [validAt, setValidAt] = useState(initialBounds.max)

  const loadLiveGraph = useCallback(async (signal?: AbortSignal) => {
    const graph = await brainHubApi.graph(undefined, signal)
    setSourceGraph(graph)
    setSearchGraph(null)
    setHits([])
    const fallbackAnchor = graph.anchorId
      ?? graph.nodes.find((node) => node.kind === 'Workstream')?.id
      ?? graph.nodes[0]?.id
      ?? ''
    setAnchorId((current) => graph.nodes.some((node) => node.id === current)
      ? current
      : fallbackAnchor)
    setNavigationPath((path) => {
      const validPath = path.filter((id) => graph.nodes.some((node) => node.id === id))
      return validPath.length ? validPath : fallbackAnchor ? [fallbackAnchor] : []
    })
    setValidAt(graphTimeBounds(graph).max)
    return graph
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    const demoPolicy = import.meta.env.VITE_BRAINHUB_DEMO ?? 'fallback'
    if (demoPolicy === 'force') {
      setConnection('demo')
      setConnectionMessage('Demo graph · local daemon bypassed')
      return () => controller.abort()
    }

    const connect = async () => {
      try {
        await brainHubApi.health(controller.signal)
        await loadLiveGraph(controller.signal)
        setConnection('live')
        setConnectionMessage('Local daemon connected')
      } catch (reason) {
        if (controller.signal.aborted) return
        if (reason instanceof BrainHubApiError && reason.status === 401) {
          setSourceGraph(EMPTY_GRAPH)
          setConnection('offline')
          setConnectionMessage('Daemon requires an API token · open connection settings')
          setError('Enter the daemon API token in connection settings to load your private graph.')
        } else if (demoPolicy === 'fallback') {
          setSourceGraph(demoGraph)
          setConnection('demo')
          setConnectionMessage('Demo graph · daemon unavailable')
        } else {
          setSourceGraph(EMPTY_GRAPH)
          setConnection('offline')
          setConnectionMessage('Local daemon unavailable')
          setError(reason instanceof Error ? reason.message : 'Unable to connect to Brain Hub.')
        }
      }
    }
    void connect()
    return () => controller.abort()
  }, [loadLiveGraph, tokenRevision])

  useEffect(() => {
    if (connection !== 'live') return
    let refreshTimer: number | undefined
    const subscription = subscribeToEvents(
      () => {
        window.clearTimeout(refreshTimer)
        refreshTimer = window.setTimeout(() => void loadLiveGraph().catch(() => setStreamOpen(false)), 250)
      },
      (status) => setStreamOpen(status === 'open'),
    )
    return () => {
      window.clearTimeout(refreshTimer)
      subscription.close()
    }
  }, [connection, loadLiveGraph, tokenRevision])

  useEffect(() => {
    if (!webGLSupported && sceneMode === '3d') {
      setSceneMode('2d')
      setConnectionMessage((message) => `${message} · WebGL fallback active`)
    }
  }, [sceneMode, webGLSupported])

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement
      const typing = target.matches('input, textarea, select, [contenteditable="true"]')
      if (event.key === '/' && !typing) {
        event.preventDefault()
        searchRef.current?.focus()
      }
      if (event.key === 'Escape') {
        setSettingsOpen(false)
        setSelectedId(undefined)
        setPath(null)
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [])

  const timeBounds = useMemo(() => graphTimeBounds(sourceGraph), [sourceGraph])
  const timeGraph = useMemo(
    () => filterAtTime(sourceGraph, new Date(validAt).toISOString()),
    [sourceGraph, validAt],
  )
  const filteredGraph = useMemo(() => {
    const nodes = timeGraph.nodes.filter((node) => node.id === anchorId || !hiddenKinds.has(node.kind))
    const ids = new Set(nodes.map((node) => node.id))
    const edges = timeGraph.edges.filter(
      (edge) =>
        !hiddenConfidence.has(edge.confidenceClass) &&
        ids.has(endpointId(edge.source)) &&
        ids.has(endpointId(edge.target)),
    )
    return { ...timeGraph, nodes, edges }
  }, [anchorId, hiddenConfidence, hiddenKinds, timeGraph])
  const neighborhood = useMemo(
    () => searchGraph ?? topDownSubgraph(filteredGraph, anchorId, hops),
    [anchorId, filteredGraph, hops, searchGraph],
  )
  const visibleGraph = useMemo(
    () => applySceneBudget(neighborhood, SCENE_BUDGET.maxNodes, SCENE_BUDGET.maxEdges, anchorId),
    [anchorId, neighborhood],
  )
  const selectedNode = sourceGraph.nodes.find((node) => node.id === selectedId)
  const anchorNode = sourceGraph.nodes.find((node) => node.id === anchorId)
  const pathEdgeIds = useMemo(() => new Set(path?.steps.map((step) => step.edge.id) ?? []), [path])

  const clearSearchProjection = useCallback(() => {
    setSearchGraph(null)
    setHits([])
    setPath(null)
  }, [])

  const executeSearch = async (event?: FormEvent) => {
    event?.preventDefault()
    if (!anchorId || !query.trim()) return
    setSearching(true)
    setError(undefined)
    try {
      if (connection === 'live') {
        const response = await brainHubApi.search({
          query,
          anchorId,
          hops,
          validAt: new Date(validAt).toISOString(),
          limit: 80,
          filters: {
            kinds: filteredGraph.nodes.map((node) => node.kind).filter((kind, index, list) => list.indexOf(kind) === index),
          },
        })
        // The REST response ranks nodes; keep the complete strict neighborhood
        // visible so intermediate evidence paths are never dropped from context.
        const projection = topDownSubgraph(filteredGraph, anchorId, hops)
        const visibleIds = new Set(projection.nodes.map((node) => node.id))
        setSearchGraph(projection)
        setHits(response.hits.filter((hit) => visibleIds.has(hit.node.id)))
      } else {
        const result = localSearch(filteredGraph, query, anchorId, hops)
        const projection = topDownSubgraph(filteredGraph, anchorId, hops)
        const visibleIds = new Set(projection.nodes.map((node) => node.id))
        setSearchGraph(projection)
        setHits(result.hits.filter((hit) => visibleIds.has(hit.node.id)).slice(0, 80))
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Search failed.')
    } finally {
      setSearching(false)
    }
  }

  const navigateToNode = (node: BrainNode) => {
    const segment = topDownPath(filteredGraph, anchorId, node.id, hops)
    setAnchorId(node.id)
    setSelectedId(node.id)
    setNavigationPath((current) => {
      const existing = current.indexOf(node.id)
      if (existing >= 0) return current.slice(0, existing + 1)
      return segment ? [...current, ...segment.slice(1)] : [node.id]
    })
    setQuery('')
    clearSearchProjection()
  }

  const navigateBreadcrumb = (nodeId: string, index: number) => {
    setAnchorId(nodeId)
    setSelectedId(nodeId)
    setNavigationPath((current) => current.slice(0, index + 1))
    setQuery('')
    clearSearchProjection()
  }

  const explainPath = async (node: BrainNode) => {
    setPathLoading(true)
    setError(undefined)
    try {
      const response = connection === 'live'
        ? await brainHubApi.path(anchorId, node.id, new Date(validAt).toISOString(), hops)
        : shortestPath(boundedSubgraph(filteredGraph, anchorId, hops), anchorId, node.id)
      if (!response) throw new Error(`No path exists within the strict ${hops}-hop neighborhood.`)
      setPath(response)
    } catch (reason) {
      setPath(null)
      setError(reason instanceof Error ? reason.message : 'Unable to explain this path.')
    } finally {
      setPathLoading(false)
    }
  }

  const changeTime = (value: number) => {
    setValidAt(value)
    clearSearchProjection()
  }

  const changeHops = (value: number) => {
    setHops(value)
    clearSearchProjection()
  }

  const toggleKind = (kind: NodeKind) => {
    setHiddenKinds((current) => {
      const next = new Set(current)
      if (next.has(kind)) next.delete(kind)
      else next.add(kind)
      return next
    })
    clearSearchProjection()
  }

  const toggleConfidence = (confidenceClass: ConfidenceClass) => {
    setHiddenConfidence((current) => {
      const next = new Set(current)
      if (next.has(confidenceClass)) next.delete(confidenceClass)
      else next.add(confidenceClass)
      return next
    })
    clearSearchProjection()
  }

  const resetFilters = () => {
    setHiddenKinds(new Set())
    setHiddenConfidence(new Set())
    clearSearchProjection()
  }

  const saveToken = (event: FormEvent) => {
    event.preventDefault()
    if (tokenDraft.trim()) setRuntimeApiToken(tokenDraft)
    setTokenDraft('')
    setSettingsOpen(false)
    setTokenRevision((revision) => revision + 1)
    setConnectionMessage('Reconnecting with session credentials…')
  }

  const clearToken = () => {
    setRuntimeApiToken()
    setTokenDraft('')
    setSettingsOpen(false)
    setTokenRevision((revision) => revision + 1)
    setConnectionMessage('Session credentials cleared · reconnecting…')
  }

  return (
    <div className="app-shell">
      <a className="skip-link" href="#graph-content">Skip to graph</a>
      <header className="topbar">
        <div className="brand">
          <div className="brand-mark" aria-hidden="true"><span /><span /><span /></div>
          <div>
            <strong>Brain Hub</strong>
            <small>Evidence-aware work memory</small>
          </div>
        </div>
        <div className="topbar__status">
          <div className={`connection-badge connection-badge--${connection}`} title={connectionMessage}>
            <span />
            {connection === 'live' ? (streamOpen ? 'Live' : 'Reconnecting') : connection}
          </div>
          <span className="status-copy">{connectionMessage}</span>
        </div>
        <div className="topbar__actions">
          <div className="scene-switcher" role="group" aria-label="Graph rendering mode">
            {(['3d', '2d', 'list'] as SceneMode[]).map((mode) => (
              <button
                key={mode}
                type="button"
                aria-pressed={sceneMode === mode}
                disabled={mode === '3d' && !webGLSupported}
                onClick={() => setSceneMode(mode)}
              >
                {mode === 'list' ? 'List' : mode.toUpperCase()}
              </button>
            ))}
          </div>
          <button
            type="button"
            className="settings-button"
            aria-label="Connection settings"
            aria-expanded={settingsOpen}
            onClick={() => setSettingsOpen((open) => !open)}
          >
            ⚙
          </button>
        </div>
      </header>

      {settingsOpen && (
        <form className="connection-settings" onSubmit={saveToken} aria-label="Connection settings">
          <header>
            <div><span className="eyebrow">This browser tab only</span><h2>Daemon access</h2></div>
            <button type="button" className="icon-button" onClick={() => setSettingsOpen(false)} aria-label="Close settings">×</button>
          </header>
          <label htmlFor="api-token">API token</label>
          <input
            id="api-token"
            type="password"
            value={tokenDraft}
            onChange={(event) => setTokenDraft(event.currentTarget.value)}
            placeholder={getApiToken() ? 'Token saved for this tab' : 'Paste a token'}
            autoComplete="off"
            spellCheck={false}
          />
          <p>Stored in sessionStorage and erased when this tab closes. It is never displayed or written to localStorage.</p>
          <div>
            <button type="button" className="secondary-button" onClick={clearToken} disabled={!getApiToken()}>Clear token</button>
            <button type="submit" className="primary-button" disabled={!tokenDraft.trim()}>Save for session</button>
          </div>
        </form>
      )}

      <main id="graph-content" className="workspace">
        <Filters
          hiddenKinds={hiddenKinds}
          hiddenConfidence={hiddenConfidence}
          onToggleKind={toggleKind}
          onToggleConfidence={toggleConfidence}
          onReset={resetFilters}
        />

        <section className="graph-workspace" aria-label="Brain Hub graph explorer">
          <div className="graph-toolbar">
            <form className="search-form" role="search" onSubmit={executeSearch}>
              <span className="search-icon" aria-hidden="true">⌕</span>
              <input
                ref={searchRef}
                value={query}
                onChange={(event) => setQuery(event.currentTarget.value)}
                placeholder="Search this neighborhood…"
                aria-label="Search the anchored graph neighborhood"
              />
              {query && (
                <button type="button" className="clear-search" aria-label="Clear search" onClick={() => {
                  setQuery('')
                  clearSearchProjection()
                }}>×</button>
              )}
              <kbd>/</kbd>
              <button type="submit" className="search-submit" disabled={searching || !anchorId || !query.trim()}>
                {searching ? 'Searching…' : 'Search'}
              </button>
            </form>
            <div className="anchor-control">
              <span className="anchor-control__pulse" aria-hidden="true" />
              <div>
                <small>Starting at</small>
                <strong>{anchorNode?.label ?? 'No anchor'}</strong>
              </div>
            </div>
            <label className="hop-control">
              <button
                type="button"
                aria-label="Collapse one hierarchy level"
                disabled={hops <= 1}
                onClick={() => changeHops(Math.max(1, hops - 1))}
              >−</button>
              <span>Visible depth</span>
              <select value={hops} onChange={(event) => changeHops(Number(event.currentTarget.value))}>
                {Array.from({ length: MAX_GRAPH_HOPS }, (_, index) => index + 1).map((depth) => (
                  <option key={depth} value={depth}>{depth} hop{depth === 1 ? '' : 's'}</option>
                ))}
              </select>
              <button
                type="button"
                aria-label="Expand one hierarchy level"
                disabled={hops >= MAX_GRAPH_HOPS}
                onClick={() => changeHops(Math.min(MAX_GRAPH_HOPS, hops + 1))}
              >+</button>
            </label>
          </div>

          <nav className="graph-breadcrumbs" aria-label="Hierarchy path">
            {navigationPath.map((nodeId, index) => {
              const node = sourceGraph.nodes.find((candidate) => candidate.id === nodeId)
              if (!node) return null
              const current = index === navigationPath.length - 1
              return (
                <span key={`${nodeId}-${index}`}>
                  {index > 0 && <span aria-hidden="true">→</span>}
                  <button
                    type="button"
                    aria-current={current ? 'page' : undefined}
                    onClick={() => navigateBreadcrumb(nodeId, index)}
                  >
                    {node.label}
                  </button>
                </span>
              )
            })}
          </nav>

          <div className="graph-meta" aria-live="polite">
            <div><strong>{visibleGraph.nodes.length}</strong><span>nodes</span></div>
            <div><strong>{visibleGraph.edges.length}</strong><span>edges</span></div>
            <div className="strict-badge"><span>↓</span> top-down · directed children</div>
            {visibleGraph.truncated && <div className="budget-badge">Scene budget applied</div>}
            {reducedMotion && <div className="budget-badge">Reduced motion</div>}
          </div>

          {hits.length > 0 && (
            <section className="search-results" aria-label="Search results">
              <header>
                <span>{hits.length} matches</span>
                <button type="button" onClick={clearSearchProjection}>Show neighborhood</button>
              </header>
              <ol>
                {hits.slice(0, 8).map((hit) => (
                  <li key={hit.node.id}>
                    <button type="button" onClick={() => navigateToNode(hit.node)}>
                      <span className="node-dot" style={{ '--node-color': KIND_COLORS[hit.node.kind] } as React.CSSProperties} />
                      <span><strong>{hit.node.label}</strong><small>{hit.reasons.join(' · ')}</small></span>
                      <em aria-label={`Relevance score ${Math.round(hit.score * 100)} percent`}>
                        {Math.round(hit.score * 100)}
                      </em>
                    </button>
                  </li>
                ))}
              </ol>
            </section>
          )}

          {visibleGraph.nodes.length > 0 ? (
            <GraphScene
              graph={visibleGraph}
              mode={sceneMode}
              anchorId={anchorId}
              selectedId={selectedId}
              pathEdgeIds={pathEdgeIds}
              reducedMotion={reducedMotion}
              onSelect={navigateToNode}
              onBackgroundClick={() => {
                setSelectedId(undefined)
                setPath(null)
              }}
            />
          ) : (
            <div className="empty-state">
              <div className="empty-orbit" aria-hidden="true"><span /></div>
              <h2>No graph signal here</h2>
              <p>{connection === 'offline' ? 'Start the Brain Hub daemon and reconnect.' : `No nodes are valid at ${formatMoment(validAt)} with these filters.`}</p>
              <button type="button" className="secondary-button" onClick={resetFilters}>Reset filters</button>
            </div>
          )}

          <Timeline
            min={timeBounds.min}
            max={timeBounds.max}
            value={validAt}
            onChange={changeTime}
            resultCount={visibleGraph.nodes.length}
          />
        </section>

        <DetailsDrawer
          node={selectedNode}
          graph={filteredGraph}
          anchorId={anchorId}
          path={path}
          pathLoading={pathLoading}
          onClose={() => {
            setSelectedId(undefined)
            setPath(null)
          }}
          onMakeAnchor={navigateToNode}
          onExplainPath={explainPath}
          onSelect={navigateToNode}
        />
      </main>

      {error && (
        <div className="toast" role="alert">
          <span>!</span>
          <p>{error}</p>
          <button type="button" aria-label="Dismiss error" onClick={() => setError(undefined)}>×</button>
        </div>
      )}
    </div>
  )
}

export default App
