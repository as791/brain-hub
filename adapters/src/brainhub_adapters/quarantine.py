"""Bounded local audit quarantine for permanently rejected adapter events."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Iterator, Mapping

from .model import canonical_json, stable_digest


@dataclass(frozen=True, slots=True)
class QuarantineResult:
    path: Path
    pruned: int
    already_present: bool


class BoundedQuarantine:
    """Store permission-restricted rejection records under count and byte bounds."""

    def __init__(
        self,
        root: str | Path,
        *,
        max_events: int = 100,
        max_bytes: int = 20 * 1024 * 1024,
    ) -> None:
        if max_events < 1 or max_bytes < 1:
            raise ValueError("quarantine bounds must be positive")
        self.root = Path(root).expanduser()
        self.max_events = max_events
        self.max_bytes = max_bytes

    def _ensure_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)

    def _files(self) -> list[Path]:
        if not self.root.is_dir():
            return []
        return sorted(
            (path for path in self.root.iterdir() if path.suffix == ".json"),
            key=lambda path: (path.stat().st_mtime_ns, path.name),
        )

    def add(
        self,
        event: Mapping[str, Any],
        *,
        http_status: int,
        original_spool_file: str,
    ) -> QuarantineResult:
        event_payload = dict(event)
        event_json = canonical_json(event_payload)
        event_sha256 = hashlib.sha256(event_json.encode("utf-8")).hexdigest()
        event_id = str(event_payload.get("id") or "unknown")
        safe_id = stable_digest(event_id, event_sha256)
        record = {
            "schema_version": "1",
            "event_id": event_id,
            "event_sha256": event_sha256,
            "quarantined_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "reason": {
                "category": "permanent_http_record_failure",
                "http_status": int(http_status),
            },
            "original_spool_file": original_spool_file,
            "event": json.loads(event_json),
        }
        payload = (canonical_json(record) + "\n").encode("utf-8")
        if len(payload) > self.max_bytes:
            raise OSError("quarantine record exceeds the configured byte bound")

        self._ensure_root()
        destination = self.root / f"rejected_{safe_id}.json"
        if destination.exists():
            return QuarantineResult(destination, pruned=0, already_present=True)

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
            prefix=f".{destination.stem}.",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, destination)
            except FileExistsError:
                return QuarantineResult(destination, pruned=pruned, already_present=True)
            os.chmod(destination, 0o600)
            return QuarantineResult(destination, pruned=pruned, already_present=False)
        finally:
            temporary.unlink(missing_ok=True)

    def records(self) -> Iterator[dict[str, Any]]:
        for path in self._files():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict):
                yield payload

    def count(self) -> int:
        return len(self._files())
