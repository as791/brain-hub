"""Best-effort spool flushing, intended for a daemon or explicit command."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import NamedTuple
from urllib import error, request

from .quarantine import BoundedQuarantine
from .spool import BoundedSpool


PERMANENT_RECORD_FAILURES = frozenset({400, 409, 413, 422})


class FlushResult(NamedTuple):
    delivered: int
    remaining: int
    error: str | None
    quarantined: int = 0
    quarantine_path: str | None = None


def default_quarantine(spool: BoundedSpool) -> BoundedQuarantine:
    configured = os.environ.get("BRAINHUB_QUARANTINE")
    root = Path(configured).expanduser() if configured else spool.root / "quarantine"
    return BoundedQuarantine(
        root,
        max_events=int(os.environ.get("BRAINHUB_QUARANTINE_MAX_EVENTS", "100")),
        max_bytes=int(
            os.environ.get("BRAINHUB_QUARANTINE_MAX_BYTES", str(20 * 1024 * 1024))
        ),
    )


def flush_spool(
    spool: BoundedSpool,
    *,
    endpoint: str = "http://127.0.0.1:8420/v1/events",
    timeout_seconds: float = 0.25,
    limit: int = 100,
    api_token: str | None = None,
    quarantine: BoundedQuarantine | None = None,
) -> FlushResult:
    selected_token = (
        api_token if api_token is not None else os.environ.get("BRAINHUB_API_TOKEN")
    )
    delivered = 0
    quarantined = 0
    failure: str | None = None
    rejected = quarantine or default_quarantine(spool)

    def quarantine_event(path, event: dict, status: int) -> bool:
        nonlocal quarantined, failure
        try:
            rejected.add(
                event,
                http_status=status,
                original_spool_file=path.name,
            )
            spool.acknowledge(path)
        except (OSError, ValueError) as exc:
            failure = f"could not quarantine rejected event: {type(exc).__name__}"
            return False
        quarantined += 1
        return True

    for path, event in spool.pending(limit=limit):
        body = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = {
            "Content-Type": "application/cloudevents+json",
            "Idempotency-Key": str(event.get("id", "")),
        }
        if selected_token:
            headers["Authorization"] = f"Bearer {selected_token}"
        call = request.Request(
            endpoint,
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            with request.urlopen(call, timeout=timeout_seconds) as response:
                if not 200 <= response.status < 300:
                    if response.status in PERMANENT_RECORD_FAILURES:
                        if quarantine_event(path, event, response.status):
                            continue
                    failure = f"Brain Hub returned HTTP {response.status}"
                    break
        except error.HTTPError as exc:
            status = int(exc.code)
            exc.close()
            if status in PERMANENT_RECORD_FAILURES:
                if quarantine_event(path, event, status):
                    continue
                break
            failure = f"Brain Hub returned HTTP {status}"
            break
        except (error.URLError, TimeoutError, OSError) as exc:
            failure = str(exc)
            break
        spool.acknowledge(path)
        delivered += 1
    remaining = sum(1 for _ in spool.pending())
    return FlushResult(
        delivered,
        remaining,
        failure,
        quarantined,
        str(rejected.root) if quarantined else None,
    )
