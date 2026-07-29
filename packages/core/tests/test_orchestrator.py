from datetime import UTC, datetime

from brainhub.orchestrator import AgentOrchestrator, OrchestratorJob


def test_codex_command_uses_supported_noninteractive_sandbox(tmp_path):
    orchestrator = AgentOrchestrator(tmp_path / "jobs")
    now = datetime.now(UTC)
    job = OrchestratorJob(
        id="job1",
        agent="codex",
        mode="work",
        workspace=str(tmp_path),
        status="queued",
        created_at=now,
        updated_at=now,
        prompt_hash="hash",
    )

    command = orchestrator._command(job, "/usr/local/bin/codex")

    assert command == [
        "/usr/local/bin/codex",
        "exec",
        "--json",
        "--ephemeral",
        "--sandbox",
        "workspace-write",
        "-C",
        str(tmp_path),
        "-",
    ]
    assert "--ask-for-approval" not in command


def test_claude_command_disables_session_persistence(tmp_path):
    orchestrator = AgentOrchestrator(tmp_path / "jobs")
    now = datetime.now(UTC)
    job = OrchestratorJob(
        id="job2", agent="claude", mode="ask", workspace=str(tmp_path), status="queued",
        created_at=now, updated_at=now, prompt_hash="hash",
    )

    command = orchestrator._command(job, "/usr/local/bin/claude")

    assert command == [
        "/usr/local/bin/claude", "--print", "--no-session-persistence",
        "--output-format", "stream-json", "--verbose", "--permission-mode", "plan",
    ]
