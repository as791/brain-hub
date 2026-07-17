import { formatMoment } from '../lib/graph'

interface TimelineProps {
  min: number
  max: number
  value: number
  onChange: (value: number) => void
  resultCount: number
}

export function Timeline({ min, max, value, onChange, resultCount }: TimelineProps) {
  const span = Math.max(1, max - min)
  const step = Math.max(60_000, Math.round(span / 500))
  const atLatest = max - value < step * 2

  return (
    <section className="timeline" aria-label="Graph valid-time control">
      <div className="timeline__label">
        <span className="eyebrow">Fourth dimension</span>
        <strong>{formatMoment(value)}</strong>
      </div>
      <div className="timeline__track">
        <span>{formatMoment(min)}</span>
        <input
          aria-label="Show graph state at this valid time"
          type="range"
          min={min}
          max={max}
          step={step}
          value={Math.min(max, Math.max(min, value))}
          onChange={(event) => onChange(Number(event.currentTarget.value))}
          style={{ '--timeline-progress': `${((value - min) / span) * 100}%` } as React.CSSProperties}
        />
        <span>{formatMoment(max)}</span>
      </div>
      <div className="timeline__actions">
        <span>{resultCount} nodes visible</span>
        <button type="button" className="subtle-button" disabled={atLatest} onClick={() => onChange(max)}>
          Latest
        </button>
      </div>
    </section>
  )
}
