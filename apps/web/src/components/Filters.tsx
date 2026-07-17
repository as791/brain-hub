import { CONFIDENCE_COLORS, KIND_COLORS } from '../lib/graph'
import { NODE_KINDS, type ConfidenceClass, type NodeKind } from '../types'

const confidenceClasses: ConfidenceClass[] = ['EXTRACTED', 'INFERRED', 'AMBIGUOUS']

interface FiltersProps {
  hiddenKinds: Set<NodeKind>
  hiddenConfidence: Set<ConfidenceClass>
  onToggleKind: (kind: NodeKind) => void
  onToggleConfidence: (confidence: ConfidenceClass) => void
  onReset: () => void
}

export function Filters({
  hiddenKinds,
  hiddenConfidence,
  onToggleKind,
  onToggleConfidence,
  onReset,
}: FiltersProps) {
  const hasFilters = hiddenKinds.size > 0 || hiddenConfidence.size > 0
  return (
    <aside className="filters-panel" aria-label="Graph filters">
      <div className="panel-heading">
        <div>
          <span className="eyebrow">Signal layers</span>
          <h2>Filters</h2>
        </div>
        <button type="button" className="text-button" onClick={onReset} disabled={!hasFilters}>
          Reset
        </button>
      </div>

      <fieldset>
        <legend>Node kinds</legend>
        <div className="filter-list">
          {NODE_KINDS.map((kind) => (
            <label key={kind}>
              <input
                type="checkbox"
                checked={!hiddenKinds.has(kind)}
                onChange={() => onToggleKind(kind)}
              />
              <span className="filter-swatch" style={{ '--swatch': KIND_COLORS[kind] } as React.CSSProperties} />
              <span>{kind}</span>
            </label>
          ))}
        </div>
      </fieldset>

      <fieldset>
        <legend>Evidence class</legend>
        <div className="filter-list filter-list--confidence">
          {confidenceClasses.map((confidence) => (
            <label key={confidence}>
              <input
                type="checkbox"
                checked={!hiddenConfidence.has(confidence)}
                onChange={() => onToggleConfidence(confidence)}
              />
              <span className="filter-swatch" style={{ '--swatch': CONFIDENCE_COLORS[confidence] } as React.CSSProperties} />
              <span>{confidence.toLocaleLowerCase()}</span>
            </label>
          ))}
        </div>
      </fieldset>

      <div className="filter-note">
        <span className="filter-note__mark">↳</span>
          <p>Search follows directed child links from the current hierarchy root.</p>
      </div>
    </aside>
  )
}
