"""Entrypoint shared by all latency-sensitive agent hooks."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from typing import IO, Any

from .normalize import normalize_capture
from .spool import BoundedSpool


MAX_STDIN_BYTES = 1_048_576


def default_spool_path() -> Path:
    configured = os.environ.get("BRAINHUB_SPOOL")
    return Path(configured).expanduser() if configured else Path.home() / ".brainhub" / "spool"


def capture_stream(
    agent: str,
    stream: IO[str],
    *,
    mode: str = "hook",
    spool: BoundedSpool | None = None,
) -> str:
    raw = stream.read(MAX_STDIN_BYTES + 1)
    if len(raw.encode("utf-8")) > MAX_STDIN_BYTES:
        return "dropped-oversize"
    payload: Any = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("hook input must be a JSON object")
    event = normalize_capture(agent, payload, mode=mode)
    queue = spool or BoundedSpool(
        default_spool_path(),
        max_events=int(os.environ.get("BRAINHUB_SPOOL_MAX_EVENTS", "1000")),
        max_bytes=int(os.environ.get("BRAINHUB_SPOOL_MAX_BYTES", str(10 * 1024 * 1024))),
    )
    return queue.enqueue(event.as_dict()).state


def main_for(agent: str) -> int:
    """Capture failures never fail the parent agent hook."""

    try:
        capture_stream(agent, sys.stdin)
    except Exception as exc:  # hook isolation is intentional
        print(f"brainhub adapter skipped event: {exc}", file=sys.stderr)
    return 0
