import type { BrainNode, GraphSnapshot } from '../types'
import { endpointId } from './graph'

export type TaskState = 'pending' | 'in-progress' | 'blocked' | 'completed' | 'cancelled'
export type TestOutcome = 'passed' | 'failed' | 'skipped' | 'unknown'

export interface TaskStatusRecord {
  node: BrainNode
  state: TaskState
  blockers: BrainNode[]
}

export interface TestResultRecord {
  node: BrainNode
  outcome: TestOutcome
  verifies: BrainNode[]
}

const text = (value: unknown) => typeof value === 'string' ? value.toLowerCase().replaceAll('_', '-') : ''

export function workStatus(snapshot: GraphSnapshot): { tasks: TaskStatusRecord[]; tests: TestResultRecord[] } {
  const nodes = new Map(snapshot.nodes.map((node) => [node.id, node]))
  const tasks = snapshot.nodes.filter((node) => node.kind === 'Task').map((node) => {
    const blockers = snapshot.edges
      .filter((edge) => edge.relation.toUpperCase() === 'BLOCKS' && endpointId(edge.target) === node.id)
      .map((edge) => nodes.get(endpointId(edge.source)))
      .filter((blocker): blocker is BrainNode => Boolean(blocker))
    const declared = text(node.metadata?.status)
    const state: TaskState = blockers.length > 0 ? 'blocked'
      : declared === 'in-progress' || declared === 'completed' || declared === 'cancelled' ? declared
      : 'pending'
    return { node, state, blockers }
  })

  const tests = snapshot.nodes
    .filter((node) => text(node.metadata?.recordType) === 'test-result' || text(node.metadata?.record_type) === 'test-result')
    .map((node) => {
      const declared = text(node.metadata?.outcome)
      const outcome: TestOutcome = declared === 'passed' || declared === 'failed' || declared === 'skipped' ? declared : 'unknown'
      const verifies = snapshot.edges
        .filter((edge) => edge.relation.toUpperCase() === 'VERIFIES' && endpointId(edge.source) === node.id)
        .map((edge) => nodes.get(endpointId(edge.target)))
        .filter((target): target is BrainNode => Boolean(target))
      return { node, outcome, verifies }
    })

  return { tasks, tests }
}
