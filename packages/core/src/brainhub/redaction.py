"""Shared credential and path detection for policy, search, and graph sync."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, Callable


# Keep this vocabulary aligned with the capture adapters. Patterns intentionally
# recognize synthetic-looking credentials too: rejecting a false positive is safer
# than durably storing a live token in an agent-memory product.
SECRET_PATTERNS = (
    re.compile(r"\b(?:sk|rk|pk|ghp|gho|ghu|ghs|ghr|github_pat)[_-][A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\b(?:npm|hf)_[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}\b", re.IGNORECASE),
    re.compile(
        r"\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|token)"
        r"\s*[:=]\s*\S+",
        re.IGNORECASE,
    ),
    re.compile(
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?"
        r"-----END [A-Z0-9 ]*PRIVATE KEY-----",
        re.DOTALL,
    ),
)
FILE_URI_RE = re.compile(r"(?i)\bfile:(?://)?/[^\s,;\"'<>{}\[\]()]+")
POSIX_ABSOLUTE_PATH_RE = re.compile(
    r"(?<![-\w.:/@\x5c])/(?!/)[^\s,;\"'<>{}\[\]()]+"
)
WINDOWS_ABSOLUTE_PATH_RE = re.compile(
    r"(?i)(?<![\w])(?:[a-z]:[\\/]|\\\\[^\\/\s,;]+[\\/])"
    r"[^\s,;\"'<>{}\[\]()]+"
)


def contains_secret(text: str) -> bool:
    return any(pattern.search(text) for pattern in SECRET_PATTERNS)


def contains_absolute_path(text: str) -> bool:
    return any(
        pattern.search(text)
        for pattern in (
            FILE_URI_RE,
            POSIX_ABSOLUTE_PATH_RE,
            WINDOWS_ABSOLUTE_PATH_RE,
        )
    )


def redact_text(text: str) -> str:
    redacted = text
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    redacted = FILE_URI_RE.sub("opaque://local-path", redacted)
    redacted = WINDOWS_ABSOLUTE_PATH_RE.sub("opaque://local-path", redacted)
    return POSIX_ABSOLUTE_PATH_RE.sub("opaque://local-path", redacted)


def redact_value(value: Any) -> Any:
    """Recursively redact string values without changing graph payload shape."""

    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {str(key): redact_value(child) for key, child in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact_value(child) for child in value]
    return value


Pseudonymizer = Callable[[str, str | bytes], str]


def _render_identifier(value: Any) -> str:
    rendered = (
        value
        if isinstance(value, str)
        else json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    return str(rendered or "")


def opaque_identifier(kind: str, value: Any, pseudonymize: Pseudonymizer) -> str:
    digest = pseudonymize(kind, _render_identifier(value))
    return f"cloud-{kind}:{digest}"


def opaque_external_id(value: Any, pseudonymize: Pseudonymizer) -> str:
    """Return an installation-scoped ID compatible with the v1 sync schema."""

    digest = pseudonymize("external", _render_identifier(value))
    return f"external:{digest[:32]}"


def opaque_evidence_value(
    kind: str, value: Any, pseudonymize: Pseudonymizer
) -> str | None:
    if value is None:
        return None
    digest = pseudonymize(kind, _render_identifier(value))
    return f"brainhub://{kind}/{digest}"


def _sanitize_evidence(
    reference: Mapping[str, Any], pseudonymize: Pseudonymizer
) -> dict[str, Any]:
    sanitized = dict(redact_value(reference))
    sanitized["evidence_id"] = opaque_identifier(
        "evidence", reference.get("evidence_id") or reference, pseudonymize
    )
    sanitized["source_event_id"] = opaque_identifier(
        "event", reference.get("source_event_id"), pseudonymize
    )
    sanitized["opaque_uri"] = opaque_evidence_value(
        "evidence", reference.get("opaque_uri"), pseudonymize
    )
    sanitized["anchor"] = opaque_evidence_value(
        "anchor", reference.get("anchor"), pseudonymize
    )
    if reference.get("content_hash") is not None:
        sanitized["content_hash"] = opaque_identifier(
            "content", reference["content_hash"], pseudonymize
        )
    return sanitized


def sanitize_sync_graph_payload(
    payload: Mapping[str, Any], pseudonymize: Pseudonymizer
) -> dict[str, Any]:
    """Make cloud graph IDs opaque, keyed, and link-consistent."""

    sanitized = dict(redact_value(payload))
    nodes: list[dict[str, Any]] = []
    for raw_node in payload.get("nodes", []):
        node = dict(redact_value(raw_node))
        node["id"] = opaque_identifier("node", raw_node.get("id"), pseudonymize)
        node["source_event_id"] = opaque_identifier(
            "event", raw_node.get("source_event_id"), pseudonymize
        )
        if raw_node.get("actor_id") is not None:
            node["actor_id"] = opaque_identifier(
                "node", raw_node["actor_id"], pseudonymize
            )
        node["external_ids"] = [
            opaque_external_id(value, pseudonymize)
            for value in raw_node.get("external_ids", [])
        ]
        if raw_node.get("content_hash") is not None:
            node["content_hash"] = opaque_identifier(
                "content", raw_node["content_hash"], pseudonymize
            )
        node["evidence"] = [
            _sanitize_evidence(reference, pseudonymize)
            for reference in raw_node.get("evidence", [])
        ]
        nodes.append(node)
    sanitized["nodes"] = nodes

    edges: list[dict[str, Any]] = []
    for raw_edge in payload.get("edges", []):
        edge = dict(redact_value(raw_edge))
        edge["id"] = opaque_identifier("edge", raw_edge.get("id"), pseudonymize)
        edge["source_id"] = opaque_identifier(
            "node", raw_edge.get("source_id"), pseudonymize
        )
        edge["target_id"] = opaque_identifier(
            "node", raw_edge.get("target_id"), pseudonymize
        )
        edge["source_event_id"] = opaque_identifier(
            "event", raw_edge.get("source_event_id"), pseudonymize
        )
        if raw_edge.get("actor_id") is not None:
            edge["actor_id"] = opaque_identifier(
                "node", raw_edge["actor_id"], pseudonymize
            )
        edge["evidence"] = [
            _sanitize_evidence(reference, pseudonymize)
            for reference in raw_edge.get("evidence", [])
        ]
        edges.append(edge)
    sanitized["edges"] = edges
    return sanitized
