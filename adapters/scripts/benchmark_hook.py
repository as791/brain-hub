#!/usr/bin/env python3
"""Measure local hook enqueue latency without contacting the daemon."""

from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
import statistics
import tempfile
import time

from brainhub_adapters.hook import capture_stream
from brainhub_adapters.spool import BoundedSpool


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=100)
    args = parser.parse_args()
    if args.iterations < 2:
        raise SystemExit("--iterations must be at least 2")
    latencies: list[float] = []
    with tempfile.TemporaryDirectory() as directory:
        spool = BoundedSpool(Path(directory), max_events=args.iterations + 1)
        for index in range(args.iterations):
            payload = json.dumps(
                {
                    "event": "agent-turn-complete",
                    "event_id": f"benchmark-{index}",
                    "thread_id": "benchmark-thread",
                    "summary": "Completed one benchmark work unit.",
                }
            )
            started = time.perf_counter_ns()
            capture_stream("codex", io.StringIO(payload), spool=spool)
            latencies.append((time.perf_counter_ns() - started) / 1_000_000)
    ordered = sorted(latencies)
    p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
    print(
        json.dumps(
            {
                "iterations": args.iterations,
                "median_ms": round(statistics.median(latencies), 3),
                "p95_ms": round(p95, 3),
                "max_ms": round(max(latencies), 3),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
