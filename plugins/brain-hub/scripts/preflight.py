#!/usr/bin/env python3
"""Check the local plugin prerequisites without changing user state."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
from urllib import error, request


def _installed_root() -> Path | None:
    plugin_root = Path(os.environ.get("PLUGIN_ROOT", Path(__file__).resolve().parents[1]))
    if plugin_root.parent.name == "plugins" and plugin_root.parent.parent.name == "marketplace":
        return plugin_root.parents[2]
    return None


def _managed_executable(root: Path) -> Path | None:
    current = root / "runtime" / "current.json"
    if current.is_file():
        try:
            configured = Path(json.loads(current.read_text(encoding="utf-8"))["brainhub"])
            resolved_root = root.resolve()
            resolved = configured.expanduser().resolve()
            if resolved.is_relative_to(resolved_root) and os.access(resolved, os.X_OK):
                return resolved
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
            pass

    relative = (
        Path("venv") / "Scripts" / "brainhub.exe"
        if os.name == "nt"
        else Path("venv") / "bin" / "brainhub"
    )
    legacy = root / relative
    return legacy if os.access(legacy, os.X_OK) else None


def main() -> int:
    installed_root = _installed_root()
    root = installed_root or Path.home() / ".local" / "share" / "brainhub"
    managed_executable = _managed_executable(root)
    path_executable = None if installed_root else shutil.which("brainhub")
    executable = str(managed_executable) if managed_executable else path_executable
    health = "unavailable"
    if executable:
        try:
            headers: dict[str, str] = {}
            token = os.environ.get("BRAINHUB_API_TOKEN")
            if token:
                headers["Authorization"] = f"Bearer {token}"
            check = request.Request(
                "http://127.0.0.1:8420/healthz",
                headers=headers,
                method="GET",
            )
            with request.urlopen(check, timeout=0.3) as response:
                health = "healthy" if 200 <= response.status < 300 else f"http-{response.status}"
        except (error.URLError, TimeoutError, OSError):
            health = "not-running"
    print(
        json.dumps(
            {
                "brainhub_executable": bool(executable),
                "daemon": health,
                "runtime": (
                    "managed"
                    if managed_executable and executable == str(managed_executable)
                    else "path"
                    if executable
                    else "missing"
                ),
            },
            sort_keys=True,
        )
    )
    # The stdio MCP server starts its own process against the shared SQLite
    # authority, so HTTP health is informative rather than an MCP prerequisite.
    return 0 if executable else 1


if __name__ == "__main__":
    raise SystemExit(main())
