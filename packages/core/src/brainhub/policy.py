"""Capture minimization boundary applied before anything reaches durable storage."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .models import BrainEvent, ContentLevel
from .redaction import contains_absolute_path, contains_secret


class CapturePolicyError(ValueError):
    pass


ALWAYS_FORBIDDEN_KEYS = {
    "chain_of_thought",
    "chainofthought",
    "hidden_reasoning",
    "internal_reasoning",
    "reasoning_trace",
    "credentials",
    "credential",
    "password",
    "passwd",
    "secret",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "authorization",
    "private_key",
}
RAW_CONTENT_KEYS = {
    "prompt",
    "prompts",
    "transcript",
    "transcripts",
    "messages",
    "assistant_message",
    "assistant_text",
    "user_message",
    "tool_input",
    "tool_output",
    "tool_result",
    "source_code",
    "file_content",
    "raw_content",
    "content",
}
NODE_IDENTIFIER_KEYS = {
    "id",
    "external_id",
    "external_ids",
    "actor_id",
    "content_hash",
    "creation_event_id",
    "latest_revision_id",
}
EDGE_IDENTIFIER_KEYS = {
    "id",
    "source",
    "source_id",
    "target",
    "target_id",
    "actor_id",
    "creation_event_id",
    "latest_revision_id",
}
EVIDENCE_IDENTIFIER_KEYS = {"source_event_id", "content_hash"}
TOP_LEVEL_GRAPH_IDENTIFIER_KEYS = {
    "workspace_node_id",
    "workstream_node_id",
    "run_node_id",
    "target_id",
}


def _normalized_key(value: Any) -> str:
    return str(value).strip().casefold().replace("-", "_").replace(" ", "_")


def _walk(value: Any, path: tuple[str, ...] = ()):
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = _normalized_key(key)
            child_path = (*path, normalized)
            yield child_path, normalized, child
            yield from _walk(child, child_path)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, child in enumerate(value):
            yield from _walk(child, (*path, str(index)))


def _strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            yield from _strings(child)


def _explicit_identifiers(data: Mapping[str, Any]):
    for group_name in (
        "nodes",
        "topics",
        "tasks",
        "decisions",
        "claims",
        "artifacts",
    ):
        nodes = data.get(group_name)
        if not isinstance(nodes, Sequence) or isinstance(
            nodes, (str, bytes, bytearray)
        ):
            continue
        for index, item in enumerate(nodes):
            if not isinstance(item, Mapping):
                continue
            for key in NODE_IDENTIFIER_KEYS:
                for value in _strings(item.get(key)):
                    yield f"data.{group_name}.{index}.{key}", value
            provenance = item.get("provenance")
            if isinstance(provenance, Mapping):
                for value in _strings(provenance.get("actor_id")):
                    yield f"data.{group_name}.{index}.provenance.actor_id", value
                evidence = provenance.get("evidence")
                yield from _evidence_identifiers(
                    evidence,
                    f"data.{group_name}.{index}.provenance.evidence",
                )
            yield from _evidence_identifiers(
                item.get("evidence"), f"data.{group_name}.{index}.evidence"
            )

    edge_groups: list[tuple[str, Any]] = [
        ("edges", data.get("edges")),
        ("relationships", data.get("relationships")),
    ]
    if data.get("edge") is not None:
        edge_groups.append(("edge", [data["edge"]]))
    for group_name, items in edge_groups:
        if not isinstance(items, Sequence) or isinstance(items, (str, bytes, bytearray)):
            continue
        for index, item in enumerate(items):
            if not isinstance(item, Mapping):
                continue
            for key in EDGE_IDENTIFIER_KEYS:
                for value in _strings(item.get(key)):
                    yield f"data.{group_name}.{index}.{key}", value
            yield from _evidence_identifiers(
                item.get("evidence"), f"data.{group_name}.{index}.evidence"
            )
    for key in TOP_LEVEL_GRAPH_IDENTIFIER_KEYS:
        for value in _strings(data.get(key)):
            yield f"data.{key}", value


def _evidence_identifiers(value: Any, path: str):
    if isinstance(value, Mapping):
        items = [value]
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        items = value
    else:
        return
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            continue
        for key in EVIDENCE_IDENTIFIER_KEYS:
            for identifier in _strings(item.get(key)):
                yield f"{path}.{index}.{key}", identifier


def validate_capture_policy(event: BrainEvent, *, allow_raw_content: bool = False) -> None:
    data = event.data.model_dump(mode="python", exclude_none=True)
    extensions = dict(event.model_extra or {})
    envelope = {
        "id": event.id,
        "source": event.source,
        "type": event.type,
        "subject": event.subject,
    }
    content_authorized = (
        allow_raw_content and event.data.capture.content_level == ContentLevel.CONTENT
    )
    violations: list[str] = []
    for key in ("id", "source", "subject"):
        value = envelope[key]
        if contains_absolute_path(value):
            violations.append(f"absolute path identifier rejected at event.{key}")
    for path, value in _explicit_identifiers(data):
        if contains_absolute_path(value):
            violations.append(f"absolute path identifier rejected at {path}")
    for root_name, root in (
        ("event", envelope),
        ("data", data),
        ("extensions", extensions),
    ):
        for path, key, value in _walk(root, (root_name,)):
            rendered_path = ".".join(path)
            if key in ALWAYS_FORBIDDEN_KEYS:
                violations.append(f"forbidden sensitive field: {rendered_path}")
                continue
            if key in RAW_CONTENT_KEYS and not content_authorized:
                violations.append(
                    "raw content field requires content_level=content and "
                    f"BRAINHUB_ALLOW_RAW_CONTENT=true: {rendered_path}"
                )
                continue
            if isinstance(value, str) and contains_secret(value):
                violations.append(f"credential-like value rejected at {rendered_path}")
    if violations:
        raise CapturePolicyError("; ".join(violations[:10]))
