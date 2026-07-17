"""CloudEvents 1.0 contract shared by every agent adapter."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Mapping


# CloudEvents permits producers to omit ``time``, but the Brain Hub adapter
# contract requires it.  When a host does not expose an occurrence timestamp,
# use an explicit sentinel instead of capture time: capture time changes on a
# retry and would bind the same deterministic event ID to different content.
UNKNOWN_EVENT_TIME = "1970-01-01T00:00:00Z"


def canonical_json(value: Any) -> str:
    """Return the single representation used for hashes, files, and retries."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def normalized_time(value: str | None) -> str:
    if not value:
        return UNKNOWN_EVENT_TIME
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return UNKNOWN_EVENT_TIME
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def stable_digest(*parts: Any) -> str:
    body = canonical_json(parts).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


@dataclass(frozen=True, slots=True)
class CloudEvent:
    """Small value object which serializes to the Brain Hub event schema."""

    specversion: str
    id: str
    source: str
    type: str
    subject: str
    time: str
    datacontenttype: str
    data: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "specversion": self.specversion,
            "id": self.id,
            "source": self.source,
            "type": self.type,
            "subject": self.subject,
            "time": self.time,
            "datacontenttype": self.datacontenttype,
            "data": dict(self.data),
        }

    def to_json(self) -> str:
        return canonical_json(self.as_dict())


def make_event(
    *,
    source: str,
    event_type: str,
    subject: str,
    data: Mapping[str, Any],
    occurred_at: str | None = None,
    event_key: str | None = None,
) -> CloudEvent:
    """Create an idempotent CloudEvent.

    The event identifier intentionally excludes the wall-clock capture time. A hook
    retry with the same stable key or normalized payload therefore produces the same
    identifier and is deduplicated by both the spool and the daemon.
    """

    time = normalized_time(occurred_at)
    identity = event_key or stable_digest(source, event_type, subject, data)
    event_id = f"evt_{stable_digest(source, event_type, subject, identity)[:40]}"
    return CloudEvent(
        specversion="1.0",
        id=event_id,
        source=source,
        type=event_type,
        subject=subject,
        time=time,
        datacontenttype="application/json",
        data=dict(data),
    )
