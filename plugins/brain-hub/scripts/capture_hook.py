#!/usr/bin/env python3
"""Privacy-preserving, fail-open Codex hook launcher for Brain Hub."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any, Mapping


MAX_STDIN_BYTES = 1_048_576
SAFE_FIELDS = frozenset(
    {
        "agent_id",
        "agent_type",
        "cwd",
        "duration_ms",
        "event_id",
        "exit_code",
        "hook_event_name",
        "invocation_id",
        "model",
        "parent_session_id",
        "permission_mode",
        "reason",
        "session_id",
        "source",
        "status",
        "time",
        "timestamp",
        "tool_name",
        "turn_id",
        "version",
        "workspace_id",
    }
)


def sanitized_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Copy only bounded scalar metadata; never forward agent content."""

    safe: dict[str, Any] = {}
    for key in SAFE_FIELDS:
        value = payload.get(key)
        if isinstance(value, (str, int, float, bool)) and len(str(value)) <= 4096:
            safe[key] = value
    event_name = str(safe.get("hook_event_name") or "").casefold()
    agent_id = safe.get("agent_id")
    session_id = safe.get("session_id")
    if event_name in {"subagentstart", "subagentstop"} and agent_id and session_id:
        safe["parent_session_id"] = session_id
        safe["session_id"] = agent_id
    return safe


def hook_executable(plugin_root: Path) -> str | None:
    configured = os.environ.get("BRAINHUB_CODEX_HOOK")
    if configured:
        return configured

    # Installed plugins live at <install-root>/marketplace/plugins/brain-hub.
    try:
        current = plugin_root.parents[2] / "runtime" / "current.json"
        runtime = json.loads(current.read_text(encoding="utf-8"))
        venv = Path(runtime["venv"])
        candidate = venv / ("Scripts" if os.name == "nt" else "bin")
        candidate /= "brainhub-codex-hook.exe" if os.name == "nt" else "brainhub-codex-hook"
        if candidate.is_file():
            return str(candidate)
    except (IndexError, KeyError, OSError, TypeError, json.JSONDecodeError):
        pass

    installed_plugin = (
        plugin_root.parent.name == "plugins" and plugin_root.parent.parent.name == "marketplace"
    )
    if installed_plugin:
        # A managed plugin must fail closed if its verified runtime disappeared.
        return None

    discovered = shutil.which("brainhub-codex-hook")
    if discovered:
        return discovered
    managed = (
        Path.home()
        / ".local"
        / "share"
        / "brainhub"
        / "venv"
        / ("Scripts" if os.name == "nt" else "bin")
        / ("brainhub-codex-hook.exe" if os.name == "nt" else "brainhub-codex-hook")
    )
    return str(managed) if managed.is_file() else None


def main() -> int:
    """Queue safe metadata and always leave the parent Codex action unblocked."""

    try:
        raw = sys.stdin.read(MAX_STDIN_BYTES + 1)
        if len(raw.encode("utf-8")) > MAX_STDIN_BYTES:
            return 0
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return 0
        executable = hook_executable(
            Path(os.environ.get("PLUGIN_ROOT", Path(__file__).resolve().parents[1]))
        )
        if executable is None:
            return 0
        subprocess.run(
            [executable],
            input=json.dumps(sanitized_payload(payload)),
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
            check=False,
        )
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
