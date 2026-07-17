#!/usr/bin/env python3
"""Check the local plugin prerequisites without changing user state."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
from urllib import error, request


def main() -> int:
    managed_executable = Path.home() / ".local" / "share" / "brainhub" / "venv" / "bin" / "brainhub"
    path_executable = shutil.which("brainhub")
    executable = path_executable or (
        str(managed_executable) if os.access(managed_executable, os.X_OK) else None
    )
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
                    if executable == str(managed_executable)
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
