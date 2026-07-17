"""Normalize stable hook, OTLP, stream, and MCP records into one contract."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping

from .model import CloudEvent, make_event, stable_digest
from .redaction import (
    artifact_references,
    explicit_summary,
    opaque_reference,
    opaque_workspace,
    safe_metadata,
    safe_text,
)


@dataclass(frozen=True, slots=True)
class AgentProfile:
    product: str
    aliases: tuple[str, ...]


PROFILES: dict[str, AgentProfile] = {
    "codex": AgentProfile("codex", ("codex", "openai-codex")),
    "claude": AgentProfile("claude-code", ("claude", "claude-code")),
    "cursor": AgentProfile("cursor", ("cursor",)),
    "antigravity": AgentProfile("antigravity", ("antigravity", "google-antigravity")),
    "generic": AgentProfile("generic-agent", ("generic",)),
}

_START_EVENTS = {
    "sessionstart",
    "session_start",
    "thread.started",
    "run.started",
    "start",
    "started",
}
_COMPLETE_EVENTS = {
    "agent-turn-complete",
    "agent_turn_complete",
    "sessionend",
    "session_end",
    "stop",
    "task_complete",
    "turn.completed",
    "run.completed",
    "completed",
}
_FAILED_EVENTS = {"error", "failed", "failure", "run.failed", "turn.failed"}
_CANCELLED_EVENTS = {"cancel", "cancelled", "canceled", "run.cancelled"}


def _first(payload: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return value
    return None


def _otlp_value(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return value
    for key in ("stringValue", "intValue", "doubleValue", "boolValue", "string_value"):
        if key in value:
            return value[key]
    return None


def _effective_payload(payload: Mapping[str, Any], mode: str) -> Mapping[str, Any]:
    if mode != "otlp":
        return payload
    normalized: dict[str, Any] = {}
    attributes = payload.get("attributes")
    if isinstance(attributes, Mapping):
        normalized.update({str(key): _otlp_value(value) for key, value in attributes.items()})
    elif isinstance(attributes, list):
        for attribute in attributes:
            if not isinstance(attribute, Mapping) or not isinstance(attribute.get("key"), str):
                continue
            normalized[attribute["key"]] = _otlp_value(attribute.get("value"))
    aliases = {
        "brainhub.session.id": "session_id",
        "session.id": "session_id",
        "thread.id": "thread_id",
        "event.name": "event_name",
        "brainhub.summary": "brainhub_summary",
        "workspace.id": "workspace_id",
        "agent.version": "agent_version",
    }
    for source, destination in aliases.items():
        if source in normalized and destination not in normalized:
            normalized[destination] = normalized[source]
    for key in ("time", "timestamp", "event_id"):
        if key in payload:
            normalized[key] = payload[key]
    return normalized


def _event_name(payload: Mapping[str, Any]) -> str:
    value = _first(payload, "hook_event_name", "event_name", "event", "type", "name")
    redacted = safe_text(str(value or "unknown"), limit=120) or "unknown"
    normalized = re.sub(r"[^a-z0-9._-]+", "-", redacted.strip().lower()).strip(".-_")
    return normalized or "unknown"


def _status(event_name: str, payload: Mapping[str, Any]) -> str:
    explicit = str(payload.get("status") or "").strip().lower()
    candidate = explicit or event_name
    if candidate in _START_EVENTS:
        return "started"
    if candidate in _COMPLETE_EVENTS:
        return "completed"
    if candidate in _FAILED_EVENTS:
        return "failed"
    if candidate in _CANCELLED_EVENTS:
        return "cancelled"
    return "unknown"


def _session_id(agent: str, payload: Mapping[str, Any]) -> str:
    value = _first(
        payload,
        "session_id",
        "session.id",
        "thread_id",
        "conversation_id",
        "run_id",
    )
    if value is not None:
        return safe_text(str(value), limit=256) or f"unknown-{agent}"
    safe_identity = (
        _event_name(payload),
        _first(payload, "event_id", "hook_id", "invocation_id", "turn_id"),
        _first(payload, "workspace_id", "cwd", "workspace", "project_path"),
        _first(payload, "time", "timestamp"),
    )
    return f"unknown-{agent}-{stable_digest(agent, safe_identity)[:16]}"


def normalize_capture(
    agent: str,
    payload: Mapping[str, Any],
    *,
    mode: str = "hook",
    surface: str | None = None,
    occurred_at: str | None = None,
) -> CloudEvent:
    """Normalize one explicitly supplied record without reading external state.

    Transcript paths, prompts, messages, and arbitrary content fields are ignored by
    construction. Integrations can provide an explicit semantic summary instead.
    """

    payload = _effective_payload(payload, mode)
    profile = PROFILES.get(agent.lower(), PROFILES["generic"])
    event_name = _event_name(payload)
    status = _status(event_name, payload)
    session_id = _session_id(agent, payload)
    raw_workspace = _first(payload, "workspace_id", "cwd", "workspace", "project_path")
    workspace_id = opaque_workspace(raw_workspace)
    source_surface = surface or mode
    data: dict[str, Any] = {
        "agent": {
            "product": profile.product,
            "surface": source_surface,
        },
        "workspace_id": workspace_id,
        "session_id": session_id,
        "status": status,
        "capture": {
            "mode": mode,
            "content_level": "summary" if explicit_summary(payload) else "metadata",
            "redactions": [
                "raw-prompts",
                "messages",
                "transcripts",
                "credentials",
                "absolute-paths",
            ],
        },
    }

    version = safe_text(_first(payload, "agent_version", "version"), limit=80)
    if version:
        data["agent"]["version"] = version
    turn_id = safe_text(_first(payload, "turn_id", "hook_id", "invocation_id"), limit=256)
    if turn_id:
        data["turn_id"] = turn_id
    parent = safe_text(payload.get("parent_session_id"), limit=256)
    if parent:
        data["parent_session_id"] = parent
    summary = explicit_summary(payload)
    if summary:
        data["summary"] = summary
    artifacts = artifact_references(payload)
    if artifacts:
        data["artifacts"] = artifacts

    metadata = safe_metadata(
        payload,
        (
            "model",
            "reason",
            "exit_code",
            "duration_ms",
            "tool_name",
            "permission_mode",
        ),
    )
    metadata["source_event"] = event_name
    if metadata:
        data["metadata"] = metadata

    event_key_raw = _first(payload, "event_id", "hook_id", "invocation_id", "turn_id")
    event_key = str(event_key_raw) if event_key_raw is not None else None
    event_type = f"com.brainhub.agent.run.{status}.v1"
    return make_event(
        source=f"brainhub-adapter://{profile.product}/{source_surface}",
        event_type=event_type,
        subject=f"sessions/{opaque_reference(session_id, prefix='session')}",
        data=data,
        occurred_at=occurred_at or safe_text(_first(payload, "time", "timestamp"), limit=80),
        event_key=event_key,
    )
