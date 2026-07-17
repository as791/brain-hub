"""Graphify public JSON/CLI boundary; no Graphify internals are imported."""

from __future__ import annotations

import hashlib
import json
import subprocess
import re
from datetime import UTC, datetime
from pathlib import Path
from pathlib import PureWindowsPath
from typing import Any, Sequence

from .models import BrainEvent, ConfidenceClass, EdgeType, NodeType, stable_id


class GraphifyImportError(ValueError):
    pass


def _first(mapping: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if mapping.get(key) is not None:
            return mapping[key]
    return default


def _properties(item: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for key in ("properties", "metadata", "audit"):
        if isinstance(item.get(key), dict):
            merged.update(item[key])
    return merged


def _node_type(value: Any) -> NodeType:
    normalized = str(value or "TOPIC").upper().replace(" ", "_").replace("-", "_")
    aliases = {
        "ENTITY": NodeType.TOPIC,
        "CONCEPT": NodeType.TOPIC,
        "DOCUMENT": NodeType.ARTIFACT,
        "FILE": NodeType.ARTIFACT,
        "PERSON": NodeType.ACTOR,
        "ORGANIZATION": NodeType.ACTOR,
        "RELATIONSHIP": NodeType.CLAIM,
        "HYPEREDGE": NodeType.CLAIM,
    }
    try:
        return NodeType(normalized)
    except ValueError:
        return aliases.get(normalized, NodeType.TOPIC)


def _edge_type(value: Any) -> EdgeType:
    normalized = str(value or "REFERENCES").upper().replace(" ", "_").replace("-", "_")
    aliases = {
        "USES": EdgeType.USED,
        "USE": EdgeType.USED,
        "PRODUCES": EdgeType.PRODUCED,
        "PRODUCE": EdgeType.PRODUCED,
        "MODIFY": EdgeType.MODIFIES,
        "DEPENDS": EdgeType.DEPENDS_ON,
        "DEPENDS_UPON": EdgeType.DEPENDS_ON,
        "BLOCKED_BY": EdgeType.BLOCKS,
        "DERIVES_FROM": EdgeType.DERIVED_FROM,
        "REFERS_TO": EdgeType.REFERENCES,
        "REFERENCE": EdgeType.REFERENCES,
        "RELATED_TO": EdgeType.REFERENCES,
        "RELATES_TO": EdgeType.REFERENCES,
        "PARTICIPANT": EdgeType.PARTICIPATES_IN,
    }
    try:
        return EdgeType(normalized)
    except ValueError:
        return aliases.get(normalized, EdgeType.REFERENCES)


def _confidence(item: dict[str, Any]) -> tuple[ConfidenceClass, float]:
    value = _first(item, "confidence_class", "confidence", default="AMBIGUOUS")
    score_value = _first(item, "confidence_score", "score", default=None)
    if isinstance(value, (int, float)):
        score_value = value if score_value is None else score_value
        value = "INFERRED" if float(value) < 0.9 else "EXTRACTED"
    try:
        confidence_class = ConfidenceClass(str(value).upper())
    except ValueError:
        confidence_class = ConfidenceClass.AMBIGUOUS
    if score_value is None:
        score_value = {
            ConfidenceClass.EXTRACTED: 1.0,
            ConfidenceClass.INFERRED: 0.7,
            ConfidenceClass.AMBIGUOUS: 0.2,
        }[confidence_class]
    return confidence_class, max(0.0, min(1.0, float(score_value)))


def _safe_source_file(value: Any) -> str | None:
    if not value:
        return None
    rendered = str(value)
    path = Path(rendered)
    if path.is_absolute() or PureWindowsPath(rendered).is_absolute():
        return f"brainhub://artifact/{hashlib.sha256(rendered.encode()).hexdigest()[:32]}"
    return rendered[:512]


def _evidence(item: dict[str, Any]) -> list[dict[str, Any]]:
    properties = _properties(item)
    source_file = _safe_source_file(
        _first(item, "source_file", default=properties.get("source_file"))
    )
    source_location = _first(
        item, "source_location", "location", default=properties.get("source_location")
    )
    locator = None
    if source_file and source_location:
        locator = f"{source_file}#{source_location}"
    elif source_file or source_location:
        locator = str(source_file or source_location)
    return [{"locator": locator, "visibility": "LOCAL"}]


class GraphifyImporter:
    """Translate `graphify-out/graph.json` into an auditable BrainEvent."""

    def load_json(self, path: str | Path) -> tuple[dict[str, Any], Path, str]:
        selected = Path(path).expanduser()
        if selected.is_dir():
            conventional = selected / "graphify-out" / "graph.json"
            selected = conventional if conventional.exists() else selected / "graph.json"
        if not selected.exists():
            raise GraphifyImportError(f"Graphify JSON export not found: {selected}")
        if selected.stat().st_size > 50 * 1024 * 1024:
            raise GraphifyImportError("Graphify export exceeds the 50 MiB import limit")
        raw = selected.read_bytes()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GraphifyImportError(f"invalid Graphify JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise GraphifyImportError("Graphify export root must be an object")
        return payload, selected, hashlib.sha256(raw).hexdigest()

    def to_event(
        self,
        path: str | Path,
        *,
        workspace_id: str = "graphify-import",
        workstream_id: str | None = None,
        imported_at: datetime | None = None,
    ) -> BrainEvent:
        graph, selected, export_hash = self.load_json(path)
        metadata = graph.get("metadata") if isinstance(graph.get("metadata"), dict) else {}
        graphify_version = str(
            _first(graph, "graphify_version", "version", default=metadata.get("graphify_version", "unknown"))
        )
        source_hash = str(metadata.get("source_hash") or graph.get("source_hash") or export_hash)
        source_identity = hashlib.sha256(source_hash.encode("utf-8")).hexdigest()
        raw_nodes = graph.get("nodes") or []
        raw_edges = graph.get("edges") or graph.get("links") or []
        raw_hyperedges = graph.get("hyperedges") or []
        if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list):
            raise GraphifyImportError("Graphify nodes and edges must be arrays")
        if len(raw_nodes) > 100_000 or len(raw_edges) > 1_000_000:
            raise GraphifyImportError("Graphify export exceeds node or edge limits")
        if isinstance(raw_hyperedges, list) and len(raw_hyperedges) > 100_000:
            raise GraphifyImportError("Graphify export exceeds the hyperedge limit")

        when = imported_at or datetime.now(UTC)
        id_map: dict[str, str] = {}
        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, Any]] = []

        for index, raw in enumerate(raw_nodes):
            if not isinstance(raw, dict):
                raise GraphifyImportError(f"nodes[{index}] must be an object")
            original_id = str(_first(raw, "id", "node_id", "key", default=f"node-{index}"))
            if original_id in id_map:
                raise GraphifyImportError(f"nodes[{index}] duplicates node ID {original_id!r}")
            canonical_id = stable_id("graphify-node", source_identity, original_id)
            id_map[original_id] = canonical_id
            properties = _properties(raw)
            properties.update(
                {
                    "graphify_original_id": original_id,
                    "graphify_version": graphify_version,
                    "graphify_source_hash": source_hash,
                    "graphify_export_hash": export_hash,
                    "source_file": _safe_source_file(
                        _first(raw, "source_file", default=properties.get("source_file"))
                    ),
                    "source_location": _first(
                        raw, "source_location", "location", default=properties.get("source_location")
                    ),
                    "graphify_original_type": _first(raw, "type", "kind", "label"),
                }
            )
            nodes.append(
                {
                    "id": canonical_id,
                    "type": _node_type(_first(raw, "type", "kind", "category")).value,
                    "title": str(_first(raw, "title", "name", "label", default=original_id))[:300],
                    "summary": str(_first(raw, "summary", "description", default="Graphify node"))[:4000],
                    "external_id": stable_id(
                        "graphify-external", source_identity, original_id
                    ),
                    "properties": properties,
                    "content_hash": raw.get("content_hash"),
                    "extractor": "graphify",
                    "extractor_version": graphify_version,
                    "evidence": _evidence(raw),
                }
            )

        seen_edge_ids: set[str] = set()
        for index, raw in enumerate(raw_edges):
            if not isinstance(raw, dict):
                raise GraphifyImportError(f"edges[{index}] must be an object")
            raw_source = str(_first(raw, "source", "source_id", "from", default=""))
            raw_target = str(_first(raw, "target", "target_id", "to", default=""))
            if raw_source not in id_map or raw_target not in id_map:
                raise GraphifyImportError(
                    f"edges[{index}] references an unknown node ({raw_source!r}, {raw_target!r})"
                )
            original_relation = str(_first(raw, "relation", "type", "label", default="REFERENCES"))
            original_id = str(
                _first(raw, "id", "edge_id", default=f"graphify-edge-{index}")
            )
            if original_id in seen_edge_ids:
                raise GraphifyImportError(f"edges[{index}] duplicates edge ID {original_id!r}")
            seen_edge_ids.add(original_id)
            confidence_class, score = _confidence(raw)
            properties = _properties(raw)
            properties.update(
                {
                    "graphify_original_id": original_id,
                    "graphify_version": graphify_version,
                    "graphify_source_hash": source_hash,
                    "graphify_export_hash": export_hash,
                    "graphify_original_relation": original_relation,
                    "source_file": _safe_source_file(
                        _first(raw, "source_file", default=properties.get("source_file"))
                    ),
                    "source_location": _first(
                        raw, "source_location", "location", default=properties.get("source_location")
                    ),
                }
            )
            edges.append(
                {
                    "id": stable_id("graphify-edge", source_identity, original_id),
                    "source_id": id_map[raw_source],
                    "target_id": id_map[raw_target],
                    "relation": _edge_type(original_relation).value,
                    "explanation": str(
                        _first(raw, "explanation", "description", default=f"Graphify relation: {original_relation}")
                    )[:320],
                    "confidence_class": confidence_class.value,
                    "confidence_score": score,
                    "properties": properties,
                    "extractor": "graphify",
                    "extractor_version": graphify_version,
                    "evidence": _evidence(raw),
                }
            )

        if not isinstance(raw_hyperedges, list):
            raise GraphifyImportError("Graphify hyperedges must be an array")
        seen_hyperedge_ids: set[str] = set()
        for index, raw in enumerate(raw_hyperedges):
            if not isinstance(raw, dict):
                raise GraphifyImportError(f"hyperedges[{index}] must be an object")
            original_id = str(_first(raw, "id", "hyperedge_id", default=f"hyperedge-{index}"))
            if original_id in seen_hyperedge_ids:
                raise GraphifyImportError(
                    f"hyperedges[{index}] duplicates hyperedge ID {original_id!r}"
                )
            seen_hyperedge_ids.add(original_id)
            hyperedge_id = stable_id(
                "graphify-hyperedge", source_identity, original_id
            )
            label = str(_first(raw, "title", "name", "label", "relation", default="Graphify hyperedge"))
            confidence_class, score = _confidence(raw)
            properties = _properties(raw)
            properties.update(
                {
                    "reified_hyperedge": True,
                    "graphify_original_id": original_id,
                    "graphify_hyperedge_id": original_id,
                    "graphify_version": graphify_version,
                    "graphify_source_hash": source_hash,
                    "graphify_export_hash": export_hash,
                    "source_file": _safe_source_file(
                        _first(raw, "source_file", default=properties.get("source_file"))
                    ),
                    "source_location": _first(
                        raw, "source_location", "location", default=properties.get("source_location")
                    ),
                }
            )
            nodes.append(
                {
                    "id": hyperedge_id,
                    "type": NodeType.CLAIM.value,
                    "title": label[:300],
                    "summary": str(_first(raw, "summary", "description", default=label))[:4000],
                    "external_id": stable_id(
                        "graphify-external", source_identity, original_id
                    ),
                    "properties": properties,
                    "extractor": "graphify",
                    "extractor_version": graphify_version,
                    "evidence": _evidence(raw),
                }
            )
            participants = _first(raw, "participants", "nodes", "members", default=[])
            for participant_index, participant in enumerate(participants):
                raw_participant = str(
                    _first(participant, "id", "node_id", default="")
                    if isinstance(participant, dict)
                    else participant
                )
                if raw_participant not in id_map:
                    raise GraphifyImportError(
                        f"hyperedges[{index}] references unknown participant {raw_participant!r}"
                    )
                edges.append(
                    {
                        "id": stable_id(
                            "graphify-participant-edge",
                            source_identity,
                            original_id,
                            raw_participant,
                            participant_index,
                        ),
                        "source_id": id_map[raw_participant],
                        "target_id": hyperedge_id,
                        "relation": EdgeType.PARTICIPATES_IN.value,
                        "explanation": "This Graphify node participates in the reified hyperedge.",
                        "confidence_class": confidence_class.value,
                        "confidence_score": score,
                        "properties": {
                            "graphify_hyperedge_id": original_id,
                            "graphify_version": graphify_version,
                            "graphify_source_hash": source_hash,
                        },
                        "extractor": "graphify",
                        "extractor_version": graphify_version,
                        "evidence": _evidence(raw),
                    }
                )

        workstream_scope = (
            stable_id("graphify-workstream", workstream_id)
            if workstream_id
            else source_identity[:16]
        )
        return BrainEvent.create(
            source=f"urn:brainhub-importer:graphify:{graphify_version}",
            type="com.brainhub.graph.imported.v1",
            subject=f"graphify/{workstream_scope}",
            time=when,
            data={
                "agent": {"product": "graphify", "surface": "public-json", "version": graphify_version},
                "workspace_id": workspace_id,
                "session_id": source_identity,
                "status": "completed",
                "capture": {"mode": "import", "content_level": "summary"},
                "nodes": nodes,
                "edges": edges,
                "import_audit": {
                    "graphify_version": graphify_version,
                    "source_hash": source_hash,
                    "export_hash": export_hash,
                    "export_file": selected.name,
                    "node_count": len(nodes),
                    "edge_count": len(edges),
                },
            },
        )
    def run_cli(
        self,
        source: str | Path,
        output_dir: str | Path,
        *,
        binary: str = "graphify",
        extra_args: Sequence[str] = (),
        timeout_seconds: int = 300,
    ) -> Path:
        """Invoke the public CLI, then return its conventional JSON export path."""

        destination = Path(output_dir)
        destination.mkdir(parents=True, exist_ok=True)
        try:
            version_result = subprocess.run(
                [binary, "--version"],
                check=True,
                timeout=10,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise GraphifyImportError(f"Graphify CLI version check failed: {exc}") from exc
        match = re.search(r"(\d+)\.(\d+)\.(\d+)", version_result.stdout + version_result.stderr)
        if not match or tuple(map(int, match.groups())) < (0, 8, 0):
            raise GraphifyImportError("Graphify CLI 0.8.0 or newer is required")
        command = self.build_cli_command(
            source, destination, binary=binary, extra_args=extra_args
        )
        try:
            subprocess.run(
                command,
                check=True,
                timeout=max(1, min(timeout_seconds, 1800)),
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise GraphifyImportError(f"Graphify CLI failed: {exc}") from exc
        candidate = destination / "graphify-out" / "graph.json"
        if not candidate.exists():
            candidate = destination / "graph.json"
        if not candidate.exists():
            raise GraphifyImportError("Graphify CLI did not produce graphify-out/graph.json")
        return candidate

    @staticmethod
    def build_cli_command(
        source: str | Path,
        output_dir: str | Path,
        *,
        binary: str = "graphify",
        extra_args: Sequence[str] = (),
    ) -> list[str]:
        return [binary, "extract", str(source), "--out", str(output_dir), *extra_args]
