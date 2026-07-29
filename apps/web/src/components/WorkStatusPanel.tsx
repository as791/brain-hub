import type { BrainNode, GraphSnapshot } from '../types'
import { workStatus } from '../lib/workStatus'

interface Props { graph: GraphSnapshot; onSelect: (node: BrainNode) => void }

export function WorkStatusPanel({ graph, onSelect }: Props) {
  const { tasks, tests } = workStatus(graph)
  const active = tasks.filter((task) => task.state !== 'completed' && task.state !== 'cancelled')
  if (active.length === 0 && tests.length === 0) return null

  return (
    <section className="work-status" aria-label="Pending work and blockers">
      <header><strong>Work status</strong><span>{active.length} pending · {tests.length} test results</span></header>
      {active.map((task) => (
        <button key={task.node.id} type="button" onClick={() => onSelect(task.node)}>
          <span data-state={task.state}>{task.state}</span>
          <strong>{task.node.label}</strong>
          <small>{task.blockers.length ? `Blocked by ${task.blockers.map((node) => node.label).join(', ')}` : task.node.summary || 'No blocker recorded'}</small>
        </button>
      ))}
      {tests.map((test) => (
        <button key={test.node.id} type="button" onClick={() => onSelect(test.node)}>
          <span data-state={test.outcome}>{test.outcome}</span>
          <strong>{test.node.label}</strong>
          <small>{test.verifies.length ? `Verifies ${test.verifies.map((node) => node.label).join(', ')}` : 'No verified task linked'}</small>
        </button>
      ))}
    </section>
  )
}
