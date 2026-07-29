import { useEffect, useState, type FormEvent } from 'react'
import { brainHubApi } from '../api'
import type { AgentMode, AgentName, OrchestratorCapabilities, OrchestratorJob } from '../types'

interface Props { anchorId?: string; hops: number; onClose: () => void; onError: (message: string) => void }

export function OrchestratorPanel({ anchorId, hops, onClose, onError }: Props) {
  const [capabilities, setCapabilities] = useState<OrchestratorCapabilities>()
  const [prompt, setPrompt] = useState('')
  const [agent, setAgent] = useState<AgentName>('codex')
  const [mode, setMode] = useState<AgentMode>('ask')
  const [workspace, setWorkspace] = useState('')
  const [copies, setCopies] = useState(1)
  const [jobs, setJobs] = useState<OrchestratorJob[]>([])
  const [submitting, setSubmitting] = useState(false)

  useEffect(() => {
    void brainHubApi.orchestratorCapabilities().then((value) => {
      setCapabilities(value); setWorkspace(value.defaultWorkspace)
      if (!value.agents.codex && value.agents.claude) setAgent('claude')
    }).catch((reason) => onError(reason instanceof Error ? reason.message : 'Unable to load agent capabilities.'))
  }, [onError])

  useEffect(() => {
    if (!jobs.some((job) => job.status === 'queued' || job.status === 'running')) return
    const timer = window.setInterval(() => {
      void Promise.all(jobs.map((job) => brainHubApi.orchestratorJob(job.id)))
        .then(setJobs).catch(() => undefined)
    }, 1000)
    return () => window.clearInterval(timer)
  }, [jobs])

  const submit = async (event: FormEvent) => {
    event.preventDefault(); setSubmitting(true)
    try {
      setJobs(await brainHubApi.startOrchestrator({ prompt, agent, mode, workspace, copies, anchorId, hops }))
      setPrompt('')
    } catch (reason) { onError(reason instanceof Error ? reason.message : 'Unable to start agent work.') }
    finally { setSubmitting(false) }
  }

  return <aside className="orchestrator-panel" aria-label="Brain Hub agent orchestrator">
    <header><div><span className="eyebrow">Local agent control plane</span><h2>Ask Brain Hub</h2></div><button className="icon-button" onClick={onClose} aria-label="Close orchestrator">×</button></header>
    <p className="orchestrator-intro">Start a persistent agent task with context from the focused graph neighborhood.</p>
    <form onSubmit={submit}>
      <textarea value={prompt} onChange={(event) => setPrompt(event.currentTarget.value)} placeholder="What should the agent investigate or build?" autoFocus />
      <div className="orchestrator-grid">
        <label>Agent<select value={agent} onChange={(event) => setAgent(event.currentTarget.value as AgentName)}>{(['codex', 'claude'] as AgentName[]).map((name) => <option key={name} value={name} disabled={!capabilities?.agents[name]}>{name}</option>)}</select></label>
        <label>Execution<select value={mode} onChange={(event) => setMode(event.currentTarget.value as AgentMode)}><option value="ask">Ask / plan</option><option value="work">Start work</option></select></label>
        <label className="workspace-field">Workspace<input value={workspace} onChange={(event) => setWorkspace(event.currentTarget.value)} /></label>
        <label>Agents<select value={copies} onChange={(event) => setCopies(Number(event.currentTarget.value))}>{[1,2,3,4].map((count) => <option key={count}>{count}</option>)}</select></label>
      </div>
      {mode === 'work' && <p className="work-warning">Work mode lets the agent edit files inside this workspace. Shell access remains sandboxed.</p>}
      <button className="primary-button" disabled={submitting || !prompt.trim() || !workspace.trim()}>{submitting ? 'Starting…' : mode === 'ask' ? 'Ask agent' : 'Start task'}</button>
    </form>
    <section className="orchestrator-jobs" aria-live="polite">{jobs.map((job) => <article key={job.id}><header><strong>{job.agent} · agent {job.copyIndex}</strong><span data-status={job.status}>{job.status}</span></header>{job.output ? <pre>{job.output}</pre> : <p>Agent session is starting…</p>}</article>)}</section>
  </aside>
}
