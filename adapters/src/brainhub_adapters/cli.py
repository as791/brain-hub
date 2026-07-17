"""CLI for adapter capture, inspection, and isolated spool delivery."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import threading

from .deliver import flush_spool
from .hook import capture_stream, default_spool_path
from .normalize import PROFILES, normalize_capture
from .spool import BoundedSpool
from .watch import WatchCycle, WatchSettings, stop_on_signals, watch_spool


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="brainhub-adapter")
    root.add_argument("--spool", type=Path, default=default_spool_path())
    commands = root.add_subparsers(dest="command", required=True)

    capture = commands.add_parser("capture", help="queue one JSON record from stdin")
    capture.add_argument("--agent", choices=sorted(PROFILES), required=True)
    capture.add_argument(
        "--mode",
        choices=("hook", "otlp", "stream", "mcp", "manual", "import"),
        default="hook",
    )

    normalize = commands.add_parser("normalize", help="print normalized event without writing")
    normalize.add_argument("--agent", choices=sorted(PROFILES), required=True)
    normalize.add_argument(
        "--mode",
        choices=("hook", "otlp", "stream", "mcp", "manual", "import"),
        default="hook",
    )

    flush = commands.add_parser("flush", help="deliver queued events to the local daemon")
    flush.add_argument("--endpoint", default="http://127.0.0.1:8420/v1/events")
    flush.add_argument("--timeout", type=float, default=0.25)
    flush.add_argument("--limit", type=int, default=100)

    watch = commands.add_parser(
        "watch",
        help="continuously deliver the spool; run under a process supervisor",
    )
    watch.add_argument(
        "--endpoint",
        default=os.environ.get(
            "BRAINHUB_ENDPOINT", "http://127.0.0.1:8420/v1/events"
        ),
    )
    watch.add_argument("--token-env", default="BRAINHUB_API_TOKEN")
    watch.add_argument("--timeout", type=float, default=0.25)
    watch.add_argument("--limit", type=int, default=100)
    watch.add_argument("--poll-interval", type=float, default=1.0)
    watch.add_argument("--initial-backoff", type=float, default=0.5)
    watch.add_argument("--max-backoff", type=float, default=30.0)
    watch.add_argument("--jitter", type=float, default=0.2)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    spool = BoundedSpool(args.spool)
    if args.command == "capture":
        try:
            capture_stream(args.agent, sys.stdin, mode=args.mode, spool=spool)
        except Exception as exc:
            print(f"brainhub adapter skipped event: {exc}", file=sys.stderr)
        return 0
    if args.command == "normalize":
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise SystemExit("input must be a JSON object")
        print(normalize_capture(args.agent, payload, mode=args.mode).to_json())
        return 0
    if args.command == "watch":
        try:
            settings = WatchSettings(
                endpoint=args.endpoint,
                api_token=os.environ.get(args.token_env),
                timeout_seconds=args.timeout,
                limit=args.limit,
                poll_interval_seconds=args.poll_interval,
                initial_backoff_seconds=args.initial_backoff,
                max_backoff_seconds=args.max_backoff,
                jitter_ratio=args.jitter,
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        stop = threading.Event()

        def report(cycle: WatchCycle) -> None:
            if cycle.result.quarantined:
                print(
                    json.dumps(
                        {
                            "quarantined": cycle.result.quarantined,
                            "quarantine_path": cycle.result.quarantine_path,
                        },
                        sort_keys=True,
                    ),
                    file=sys.stderr,
                )
            if cycle.result.error is not None:
                print(
                    json.dumps(
                        {
                            "delivery_error": cycle.result.error,
                            "remaining": cycle.result.remaining,
                            "retry_in_seconds": round(cycle.delay_seconds, 3),
                        },
                        sort_keys=True,
                    ),
                    file=sys.stderr,
                )

        with stop_on_signals(stop):
            watch_spool(spool, settings, stop_event=stop, on_cycle=report)
        return 0
    result = flush_spool(
        spool,
        endpoint=args.endpoint,
        timeout_seconds=args.timeout,
        limit=args.limit,
    )
    print(json.dumps(result._asdict(), sort_keys=True))
    return 0 if result.error is None else 1
