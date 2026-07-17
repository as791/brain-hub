"""Foreground delivery worker for the latency-isolated adapter spool."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import random
import signal
import threading
from types import FrameType
from typing import Callable, Iterator

from .deliver import FlushResult, flush_spool
from .spool import BoundedSpool


Flush = Callable[..., FlushResult]
Wait = Callable[[float], bool]


@dataclass(frozen=True, slots=True)
class WatchSettings:
    endpoint: str = "http://127.0.0.1:8420/v1/events"
    api_token: str | None = None
    timeout_seconds: float = 0.25
    limit: int = 100
    poll_interval_seconds: float = 1.0
    initial_backoff_seconds: float = 0.5
    max_backoff_seconds: float = 30.0
    jitter_ratio: float = 0.2

    def __post_init__(self) -> None:
        if not self.endpoint:
            raise ValueError("watch endpoint must not be empty")
        if self.timeout_seconds <= 0:
            raise ValueError("watch timeout must be positive")
        if self.limit < 1:
            raise ValueError("watch delivery limit must be positive")
        if self.poll_interval_seconds <= 0:
            raise ValueError("watch poll interval must be positive")
        if self.initial_backoff_seconds <= 0 or self.max_backoff_seconds <= 0:
            raise ValueError("watch backoff values must be positive")
        if self.initial_backoff_seconds > self.max_backoff_seconds:
            raise ValueError("initial watch backoff must not exceed maximum backoff")
        if not 0 <= self.jitter_ratio <= 1:
            raise ValueError("watch jitter ratio must be between zero and one")


@dataclass(frozen=True, slots=True)
class WatchCycle:
    result: FlushResult
    consecutive_failures: int
    delay_seconds: float


def _retry_delay(
    failures_before_cycle: int,
    settings: WatchSettings,
    *,
    random_value: Callable[[], float],
) -> float:
    exponential = settings.initial_backoff_seconds * (
        2 ** min(failures_before_cycle, 30)
    )
    capped = min(settings.max_backoff_seconds, exponential)
    unit = max(0.0, min(1.0, float(random_value())))
    factor = (1 - settings.jitter_ratio) + (2 * settings.jitter_ratio * unit)
    return min(settings.max_backoff_seconds, max(0.001, capped * factor))


def run_watch_cycle(
    spool: BoundedSpool,
    settings: WatchSettings,
    *,
    consecutive_failures: int = 0,
    flusher: Flush = flush_spool,
    random_value: Callable[[], float] = random.random,
) -> WatchCycle:
    """Flush once and deterministically calculate the next supervisor wait."""

    try:
        result = flusher(
            spool,
            endpoint=settings.endpoint,
            timeout_seconds=settings.timeout_seconds,
            limit=settings.limit,
            api_token=settings.api_token,
        )
    except Exception as exc:  # keep the foreground worker alive on local I/O faults
        result = FlushResult(0, 0, f"{type(exc).__name__}: {exc}")

    if result.error is not None:
        return WatchCycle(
            result=result,
            consecutive_failures=consecutive_failures + 1,
            delay_seconds=_retry_delay(
                consecutive_failures,
                settings,
                random_value=random_value,
            ),
        )
    return WatchCycle(
        result=result,
        consecutive_failures=0,
        delay_seconds=(0.0 if result.remaining else settings.poll_interval_seconds),
    )


def watch_spool(
    spool: BoundedSpool,
    settings: WatchSettings,
    *,
    stop_event: threading.Event | None = None,
    flusher: Flush = flush_spool,
    wait: Wait | None = None,
    random_value: Callable[[], float] = random.random,
    on_cycle: Callable[[WatchCycle], None] | None = None,
) -> int:
    """Deliver until stopped; return the number of attempted flush cycles."""

    stop = stop_event or threading.Event()
    waiter = wait or stop.wait
    failures = 0
    cycles = 0
    while not stop.is_set():
        cycle = run_watch_cycle(
            spool,
            settings,
            consecutive_failures=failures,
            flusher=flusher,
            random_value=random_value,
        )
        cycles += 1
        failures = cycle.consecutive_failures
        if on_cycle is not None:
            on_cycle(cycle)
        if waiter(cycle.delay_seconds):
            break
    return cycles


@contextmanager
def stop_on_signals(stop_event: threading.Event) -> Iterator[None]:
    """Translate SIGINT/SIGTERM into a clean worker shutdown and restore handlers."""

    previous: dict[signal.Signals, signal.Handlers] = {}

    def request_stop(_signum: int, _frame: FrameType | None) -> None:
        stop_event.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, request_stop)
        except ValueError:
            # Signal handlers can only be installed from the main interpreter thread.
            previous.pop(signum, None)
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
