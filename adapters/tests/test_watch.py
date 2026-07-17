from __future__ import annotations

import signal
import threading
import unittest

from brainhub_adapters.deliver import FlushResult
from brainhub_adapters.watch import (
    WatchSettings,
    run_watch_cycle,
    stop_on_signals,
    watch_spool,
)


class WatchTests(unittest.TestCase):
    def test_cycle_passes_endpoint_token_and_uses_bounded_jitter(self) -> None:
        calls: list[dict] = []

        def fail(_spool, **kwargs):
            calls.append(kwargs)
            return FlushResult(0, 3, "offline")

        settings = WatchSettings(
            endpoint="http://127.0.0.1:9999/v1/events",
            api_token="test-token",
            timeout_seconds=0.4,
            limit=7,
            initial_backoff_seconds=2,
            max_backoff_seconds=5,
            jitter_ratio=0.25,
        )
        cycle = run_watch_cycle(
            object(),  # type: ignore[arg-type]
            settings,
            consecutive_failures=2,
            flusher=fail,
            random_value=lambda: 1.0,
        )
        self.assertEqual(cycle.consecutive_failures, 3)
        self.assertEqual(cycle.delay_seconds, 5.0)
        self.assertEqual(calls[0]["endpoint"], settings.endpoint)
        self.assertEqual(calls[0]["api_token"], "test-token")
        self.assertEqual(calls[0]["timeout_seconds"], 0.4)
        self.assertEqual(calls[0]["limit"], 7)

    def test_worker_retries_then_drains_without_sleeping_between_full_batches(self) -> None:
        results = iter(
            (
                FlushResult(0, 2, "offline"),
                FlushResult(2, 1, None),
                FlushResult(1, 0, None),
            )
        )
        delays: list[float] = []

        def flush(_spool, **_kwargs):
            return next(results)

        def wait(delay: float) -> bool:
            delays.append(delay)
            return len(delays) == 3

        cycles = watch_spool(
            object(),  # type: ignore[arg-type]
            WatchSettings(
                poll_interval_seconds=3,
                initial_backoff_seconds=1,
                jitter_ratio=0,
            ),
            flusher=flush,
            wait=wait,
        )
        self.assertEqual(cycles, 3)
        self.assertEqual(delays, [1.0, 0.0, 3.0])

    def test_pre_stopped_worker_never_flushes(self) -> None:
        stop = threading.Event()
        stop.set()
        called = False

        def flush(_spool, **_kwargs):
            nonlocal called
            called = True
            return FlushResult(0, 0, None)

        cycles = watch_spool(
            object(),  # type: ignore[arg-type]
            WatchSettings(),
            stop_event=stop,
            flusher=flush,
        )
        self.assertEqual(cycles, 0)
        self.assertFalse(called)

    def test_signal_handler_requests_stop_and_is_restored(self) -> None:
        stop = threading.Event()
        previous = signal.getsignal(signal.SIGTERM)
        with stop_on_signals(stop):
            installed = signal.getsignal(signal.SIGTERM)
            self.assertTrue(callable(installed))
            installed(signal.SIGTERM, None)  # type: ignore[misc]
            self.assertTrue(stop.is_set())
        self.assertIs(signal.getsignal(signal.SIGTERM), previous)


if __name__ == "__main__":
    unittest.main()
