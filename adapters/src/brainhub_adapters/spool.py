"""A bounded, deduplicating disk spool designed for latency-sensitive hooks."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Iterator, Mapping

from .model import canonical_json, stable_digest


@dataclass(frozen=True, slots=True)
class SpoolResult:
    event_id: str
    state: str
    path: Path | None = None
    pruned: int = 0


class BoundedSpool:
    """Store one canonical event per file without waiting for network services.

    Enqueue uses an atomic rename and deterministic filename. Concurrent retries are
    harmless; the daemon also enforces event-id idempotency. Oldest files are pruned
    before a new event could exceed either configured bound.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        max_events: int = 1_000,
        max_bytes: int = 10 * 1024 * 1024,
    ) -> None:
        if max_events < 1 or max_bytes < 1:
            raise ValueError("spool bounds must be positive")
        self.root = Path(root).expanduser()
        self.max_events = max_events
        self.max_bytes = max_bytes

    def _files(self) -> list[Path]:
        if not self.root.is_dir():
            return []
        return sorted(
            (path for path in self.root.iterdir() if path.suffix == ".json"),
            key=lambda path: (path.stat().st_mtime_ns, path.name),
        )

    def enqueue(self, event: Mapping[str, Any]) -> SpoolResult:
        raw_id = event.get("id")
        event_id = str(raw_id or f"evt_{stable_digest(event)[:40]}")
        # CloudEvents IDs are opaque strings. Hash the complete value instead of
        # stripping punctuation: IDs such as ``run:a/b`` and ``runab`` must never
        # alias the same spool file.
        safe_id = f"event_{stable_digest(event_id)}"
        payload = (canonical_json(dict(event)) + "\n").encode("utf-8")
        if len(payload) > self.max_bytes:
            return SpoolResult(event_id=event_id, state="dropped-oversize")

        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        destination = self.root / f"{safe_id}.json"
        if destination.exists():
            try:
                existing_payload = destination.read_bytes()
            except OSError:
                existing_payload = b""
            state = "duplicate" if existing_payload == payload else "conflict"
            return SpoolResult(event_id=event_id, state=state, path=destination)

        files = self._files()
        total_bytes = sum(path.stat().st_size for path in files)
        pruned = 0
        while files and (
            len(files) >= self.max_events or total_bytes + len(payload) > self.max_bytes
        ):
            oldest = files.pop(0)
            try:
                size = oldest.stat().st_size
                oldest.unlink()
                total_bytes -= size
                pruned += 1
            except FileNotFoundError:
                continue

        descriptor, temporary_name = tempfile.mkstemp(
            dir=self.root,
            prefix=f".{safe_id}.",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        try:
            fchmod = getattr(os, "fchmod", None)
            if fchmod is not None:
                fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, destination)
            except FileExistsError:
                try:
                    existing_payload = destination.read_bytes()
                except OSError:
                    existing_payload = b""
                state = "duplicate" if existing_payload == payload else "conflict"
                return SpoolResult(
                    event_id=event_id,
                    state=state,
                    path=destination,
                    pruned=pruned,
                )
            return SpoolResult(
                event_id=event_id,
                state="queued",
                path=destination,
                pruned=pruned,
            )
        finally:
            temporary.unlink(missing_ok=True)

    def pending(self, *, limit: int | None = None) -> Iterator[tuple[Path, dict[str, Any]]]:
        files = self._files()
        if limit is not None:
            files = files[: max(0, limit)]
        for path in files:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict):
                yield path, payload

    def acknowledge(self, path: Path) -> None:
        resolved_root = self.root.resolve()
        resolved = path.resolve()
        if resolved.parent != resolved_root or resolved.suffix != ".json":
            raise ValueError("acknowledgement path is outside the spool")
        resolved.unlink(missing_ok=True)
