"""Versioned deterministic projection from events to typed graph facts."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any, Iterable

from .models import (
    BrainEvent,
    ConfidenceClass,
    Edge,
    EdgeType,
    EvidenceRef,
    Node,
    NodeType,
    Provenance,
    ReviewState,
    Sensitivity,
    TimeRange,
    stable_id,
)
from .redaction import redact_text
from .store import EventStore


SUPPORTED_EVENT_TYPES = {
    "com.brainhub.workstream.started.v1",
    "com.brainhub.workstream.turn.completed.v1",
    "com.brainhub.artifact.produced.v1",
    "com.brainhub.workstream.completed.v1",
    "com.brainhub.workstream.failed.v1",
    "com.brainhub.relationship.asserted.v1",
    "com.brainhub.feedback.recorded.v1",
    "com.brainhub.graph.imported.v1",
    "com.brainhub.agent.run.started.v1",
    "com.brainhub.agent.run.completed.v1",
    "com.brainhub.agent.run.failed.v1",
    "com.brainhub.agent.run.cancelled.v1",
    "com.brainhub.agent.run.unknown.v1",
}


class Projector:
    NAME = "canonical-graph"
    VERSION = "1.0.0"

    def __init__(self, store: EventStore) -> None:
        self.store = store
        self._recorded_at: datetime | None = None

    def project(
        self,
        connection: sqlite3.Connection,
        event: BrainEvent,
        sequence: int,
        *,
        recorded_at: datetime,
    ) -> dict[str, Any]:
        self._recorded_at = recorded_at
        try:
            if event.type not in SUPPORTED_EVENT_TYPES:
                return {
                    "projector": self.VERSION,
                    "source_sequence": sequence,
                    "nodes": [],
                    "edges": [],
                }

            if event.type == "com.brainhub.feedback.recorded.v1":
                changed_nodes, changed_edges = self._project_feedback(connection, event)
            else:
                changed_nodes, changed_edges = self._project_graph_event(connection, event)
        finally:
            self._recorded_at = None

        def syncable_evidence(references: list[EvidenceRef]) -> list[dict[str, Any]]:
            return [
                {
                    "evidence_id": stable_id(
                        "evidence",
                        reference.source_event_id,
                        reference.locator,
                        reference.anchor,
                        reference.content_hash,
                    ),
                    "source_event_id": reference.source_event_id,
                    "opaque_uri": reference.locator,
                    "anchor": reference.anchor,
                    "content_hash": reference.content_hash,
                    "visibility": reference.visibility.value,
                }
                for reference in references
                if reference.visibility.value == "SYNCABLE" and reference.locator
            ]

        def opaque_external_ids(values: list[str]) -> list[str]:
            return [stable_id("external", value) for value in values]

        # Graph-only means semantic graph facts are useful remotely, while raw content,
        # extension properties, prompts, transcripts, and LOCAL locators never cross.
        graph_payload = {
            "projector": self.VERSION,
            "source_sequence": sequence,
            "nodes": [
                {
                    "id": node.id,
                    "type": node.type.value,
                    "title": redact_text(node.title),
                    "summary": redact_text(node.summary),
                    "sensitivity": node.sensitivity.value,
                    "review_state": node.review_state.value,
                    "valid_from": node.valid_time.start.isoformat(),
                    "valid_to": node.valid_time.end.isoformat() if node.valid_time.end else None,
                    "recorded_from": node.recorded_time.start.isoformat(),
                    "recorded_to": (
                        node.recorded_time.end.isoformat() if node.recorded_time.end else None
                    ),
                    "source_event_id": node.latest_revision_id,
                    "actor_id": node.provenance.actor_id,
                    "extractor": node.provenance.extractor,
                    "extractor_version": node.provenance.extractor_version,
                    "external_ids": opaque_external_ids(node.external_ids),
                    "content_hash": node.content_hash,
                    "evidence": syncable_evidence(node.provenance.evidence),
                }
                for node in changed_nodes
            ],
            "edges": [
                {
                    "id": edge.id,
                    "source_id": edge.source_id,
                    "target_id": edge.target_id,
                    "relation": edge.relation.value,
                    "explanation": redact_text(edge.explanation),
                    "confidence_class": edge.confidence_class.value,
                    "confidence_score": edge.confidence_score,
                    "sensitivity": edge.sensitivity.value,
                    "review_state": edge.review_state.value,
                    "valid_from": edge.valid_time.start.isoformat(),
                    "valid_to": edge.valid_time.end.isoformat() if edge.valid_time.end else None,
                    "recorded_from": edge.recorded_time.start.isoformat(),
                    "recorded_to": (
                        edge.recorded_time.end.isoformat() if edge.recorded_time.end else None
                    ),
                    "source_event_id": edge.latest_revision_id,
                    "actor_id": edge.actor_id,
                    "extractor": edge.extractor,
                    "extractor_version": edge.extractor_version,
                    "evidence": syncable_evidence(edge.evidence),
                }
                for edge in changed_edges
            ],
        }
        return graph_payload

    def _project_graph_event(
        self, connection: sqlite3.Connection, event: BrainEvent
    ) -> tuple[list[Node], list[Edge]]:
        payload = event.data.model_dump(mode="python", exclude_none=True)
        extras = event.data.model_extra or {}
        evidence = EvidenceRef(source_event_id=event.id)
        actor_id = stable_id("actor", event.data.agent.product, event.data.agent.surface)
        workspace_id = str(extras.get("workspace_node_id") or stable_id("workspace", event.data.workspace_id))
        workstream_external = str(extras.get("workstream_id") or event.subject)
        workstream_id = str(extras.get("workstream_node_id") or stable_id("workstream", workstream_external))
        run_id = str(extras.get("run_node_id") or stable_id("run", event.data.session_id))

        nodes: list[Node] = []
        edges: list[Edge] = []

        base_events = {
            "com.brainhub.workstream.started.v1",
            "com.brainhub.workstream.turn.completed.v1",
            "com.brainhub.artifact.produced.v1",
            "com.brainhub.workstream.completed.v1",
            "com.brainhub.workstream.failed.v1",
            "com.brainhub.agent.run.started.v1",
            "com.brainhub.agent.run.completed.v1",
            "com.brainhub.agent.run.failed.v1",
            "com.brainhub.agent.run.cancelled.v1",
            "com.brainhub.agent.run.unknown.v1",
        }
        if event.type in base_events:
            workspace = self._make_node(
                event,
                NodeType.WORKSPACE,
                workspace_id,
                str(extras.get("workspace_title") or "Workspace"),
                "Opaque workspace identity for captured agent work.",
                external_ids=[event.data.workspace_id],
                properties={"opaque": True},
            )
            actor = self._make_node(
                event,
                NodeType.ACTOR,
                actor_id,
                f"{event.data.agent.product} / {event.data.agent.surface}",
                "Agent product and surface that asserted this work.",
                external_ids=[
                    f"{event.data.agent.product}:{event.data.agent.surface}"
                ],
                properties={"version": event.data.agent.version},
            )
            workstream = self._make_node(
                event,
                NodeType.WORKSTREAM,
                workstream_id,
                str(extras.get("workstream_title") or extras.get("title") or event.subject),
                event.data.summary or "Registered workstream",
                external_ids=[workstream_external],
                properties={"status": event.data.status.value, "workspace_id": workspace.id},
            )
            run = self._make_node(
                event,
                NodeType.RUN,
                run_id,
                str(extras.get("run_title") or f"{event.data.agent.product} run"),
                event.data.summary or "Captured agent run",
                external_ids=[event.data.session_id],
                properties={
                    "status": event.data.status.value,
                    "turn_id": event.data.turn_id,
                    "workspace_id": workspace.id,
                },
            )
            for node in (workspace, actor, workstream, run):
                nodes.append(self._upsert_node(connection, node))
            edges.extend(
                [
                    self._upsert_edge(
                        connection,
                        self._make_edge(
                            event,
                            workstream.id,
                            run.id,
                            EdgeType.HAS_RUN,
                            "This captured agent run belongs to the workstream.",
                            "workstream-run",
                            evidence=evidence,
                            actor_id=actor.id,
                        ),
                    ),
                    self._upsert_edge(
                        connection,
                        self._make_edge(
                            event,
                            run.id,
                            actor.id,
                            EdgeType.ASSERTED_BY,
                            "This run was asserted by the identified agent surface.",
                            "run-actor",
                            evidence=evidence,
                            actor_id=actor.id,
                        ),
                    ),
                ]
            )
            if event.data.parent_session_id:
                parent_id = stable_id("run", event.data.parent_session_id)
                if connection.execute(
                    "SELECT 1 FROM nodes WHERE node_id = ?", (parent_id,)
                ).fetchone():
                    edges.append(
                        self._upsert_edge(
                            connection,
                            self._make_edge(
                                event,
                                run.id,
                                parent_id,
                                EdgeType.CONTINUES,
                                "This run continues the referenced parent agent session.",
                                "run-continuation",
                                evidence=evidence,
                                actor_id=actor.id,
                            ),
                        )
                    )

            semantic_groups: tuple[tuple[str, NodeType, EdgeType, bool], ...] = (
                ("topics", NodeType.TOPIC, EdgeType.ABOUT, False),
                ("tasks", NodeType.TASK, EdgeType.ABOUT, False),
                ("decisions", NodeType.DECISION, EdgeType.DECIDED_IN, True),
                ("claims", NodeType.CLAIM, EdgeType.REFERENCES, False),
                ("artifacts", NodeType.ARTIFACT, EdgeType.PRODUCED, False),
            )
            for field, node_type, relation, reverse in semantic_groups:
                values = payload.get(field, [])
                if field != "artifacts":
                    values = extras.get(field, values)
                for index, value in enumerate(values or []):
                    node = self._semantic_node(
                        event,
                        node_type,
                        value,
                        index,
                        identity_scope=field,
                    )
                    node = self._upsert_node(connection, node)
                    nodes.append(node)
                    source_id, target_id = (node.id, run.id) if reverse else (run.id, node.id)
                    edges.append(
                        self._upsert_edge(
                            connection,
                            self._make_edge(
                                event,
                                source_id,
                                target_id,
                                relation,
                                self._semantic_explanation(node_type, relation),
                                f"{field}:{index}",
                                evidence=evidence,
                                actor_id=actor.id,
                            ),
                        )
                    )
                    if node_type == NodeType.CLAIM:
                        edges.append(
                            self._upsert_edge(
                                connection,
                                self._make_edge(
                                    event,
                                    node.id,
                                    actor.id,
                                    EdgeType.ASSERTED_BY,
                                    "The captured agent asserted this claim.",
                                    f"claim-actor:{index}",
                                    evidence=evidence,
                                    actor_id=actor.id,
                                ),
                            )
                        )

        # Importers and deliberate MCP calls may provide already canonical graph facts.
        explicit_nodes = list(extras.get("nodes") or [])
        for index, item in enumerate(explicit_nodes):
            node = self._explicit_node(event, item, index)
            node = self._upsert_node(connection, node)
            nodes.append(node)

        explicit_edges = list(extras.get("edges") or extras.get("relationships") or [])
        if extras.get("edge"):
            explicit_edges.append(extras["edge"])
        for index, item in enumerate(explicit_edges):
            edge = self._explicit_edge(event, item, index)
            edge = self._upsert_edge(connection, edge)
            edges.append(edge)

        return self._unique_nodes(nodes), self._unique_edges(edges)

    def _project_feedback(
        self, connection: sqlite3.Connection, event: BrainEvent
    ) -> tuple[list[Node], list[Edge]]:
        extras = event.data.model_extra or {}
        target_id = str(extras.get("target_id") or "")
        verdict = str(extras.get("verdict") or "needs_review")
        note = extras.get("note")
        state = {
            "accept": ReviewState.ACCEPTED,
            "reject": ReviewState.REJECTED,
            "incorrect": ReviewState.REJECTED,
            "duplicate": ReviewState.NEEDS_REVIEW,
            "needs_review": ReviewState.NEEDS_REVIEW,
        }.get(verdict, ReviewState.NEEDS_REVIEW)
        row = connection.execute(
            "SELECT * FROM nodes WHERE node_id = ?", (target_id,)
        ).fetchone()
        if row is not None:
            node = self.store._decode_node_row(row)
            properties = {**node.properties, "last_feedback": verdict}
            if note:
                properties["feedback_note"] = str(note)[:2000]
            updated = node.model_copy(
                update={
                    "review_state": state,
                    "properties": properties,
                    "latest_revision_id": event.id,
                    "recorded_time": TimeRange(start=self._recorded_at or event.time),
                }
            )
            self.store.upsert_node(connection, updated)
            return [updated], []
        row = connection.execute(
            "SELECT * FROM edges WHERE edge_id = ?", (target_id,)
        ).fetchone()
        if row is not None:
            edge = self.store._decode_edge_row(row)
            properties = {**edge.properties, "last_feedback": verdict}
            if note:
                properties["feedback_note"] = str(note)[:2000]
            updated = edge.model_copy(
                update={
                    "review_state": state,
                    "properties": properties,
                    "latest_revision_id": event.id,
                    "recorded_time": TimeRange(start=self._recorded_at or event.time),
                }
            )
            self.store.upsert_edge(connection, updated)
            return [], [updated]
        raise ValueError(f"feedback target {target_id!r} was not found")

    def _make_node(
        self,
        event: BrainEvent,
        node_type: NodeType,
        node_id: str,
        title: str,
        summary: str,
        *,
        external_ids: list[str] | None = None,
        properties: dict[str, Any] | None = None,
        content: dict[str, Any] | None = None,
        content_hash: str | None = None,
        sensitivity: Sensitivity = Sensitivity.INTERNAL,
        review_state: ReviewState = ReviewState.UNREVIEWED,
        provenance: Provenance | None = None,
    ) -> Node:
        moment = event.time.astimezone(UTC)
        recorded = (self._recorded_at or event.time).astimezone(UTC)
        return Node(
            id=node_id,
            type=node_type,
            title=(title or node_type.value.title())[:300],
            summary=(summary or "")[:4000],
            content=content,
            properties=properties or {},
            sensitivity=sensitivity,
            review_state=review_state,
            valid_time=TimeRange(start=moment),
            recorded_time=TimeRange(start=recorded),
            provenance=provenance
            or Provenance(evidence=[EvidenceRef(source_event_id=event.id)]),
            external_ids=external_ids or [],
            content_hash=content_hash,
            creation_event_id=event.id,
            latest_revision_id=event.id,
        )

    def _semantic_node(
        self,
        event: BrainEvent,
        node_type: NodeType,
        value: Any,
        index: int,
        *,
        identity_scope: str,
    ) -> Node:
        item = value if isinstance(value, dict) else {"title": str(value)}
        title = str(item.get("title") or item.get("name") or item.get("summary") or node_type.value)
        explicit_node_id = item.get("id")
        external_id = item.get("external_id")
        content_hash = item.get("content_hash")
        if explicit_node_id:
            node_id = str(explicit_node_id)
        elif external_id:
            node_id = stable_id(node_type.value.lower(), "external", external_id)
        elif content_hash:
            node_id = stable_id(node_type.value.lower(), "content", content_hash)
        else:
            node_id = stable_id(
                node_type.value.lower(),
                "event-ordinal",
                event.id,
                identity_scope,
                index,
            )
        evidence = self._parse_evidence(item.get("evidence"), event.id)
        return self._make_node(
            event,
            node_type,
            node_id,
            title,
            str(item.get("summary") or title),
            external_ids=[str(external_id)] if external_id else [],
            properties=dict(item.get("properties") or {}),
            content=item.get("content") if isinstance(item.get("content"), dict) else None,
            content_hash=content_hash,
            sensitivity=self._enum_or(Sensitivity, item.get("sensitivity"), Sensitivity.INTERNAL),
            review_state=self._enum_or(ReviewState, item.get("review_state"), ReviewState.UNREVIEWED),
            provenance=Provenance(
                actor_id=item.get("actor_id"),
                extractor=str(item.get("extractor") or "brainhub-core"),
                extractor_version=str(item.get("extractor_version") or self.VERSION),
                evidence=evidence,
            ),
        )

    def _explicit_node(self, event: BrainEvent, item: Any, index: int) -> Node:
        if not isinstance(item, dict):
            raise ValueError(f"nodes[{index}] must be an object")
        if {"valid_time", "recorded_time", "provenance", "creation_event_id"} <= item.keys():
            return Node.model_validate(item)
        if not any(item.get(key) for key in ("title", "name", "summary")):
            raise ValueError(f"nodes[{index}] requires a semantic title")
        try:
            node_type = NodeType(str(item.get("type") or "TOPIC").upper())
        except ValueError as exc:
            raise ValueError(f"nodes[{index}] has unknown type") from exc
        return self._semantic_node(
            event,
            node_type,
            item,
            index,
            identity_scope="explicit-nodes",
        )

    def _make_edge(
        self,
        event: BrainEvent,
        source_id: str,
        target_id: str,
        relation: EdgeType,
        explanation: str,
        discriminator: str,
        *,
        evidence: EvidenceRef | list[EvidenceRef],
        actor_id: str | None = None,
        confidence_class: ConfidenceClass = ConfidenceClass.EXTRACTED,
        confidence_score: float = 1.0,
        properties: dict[str, Any] | None = None,
        sensitivity: Sensitivity = Sensitivity.INTERNAL,
        review_state: ReviewState = ReviewState.UNREVIEWED,
        extractor: str = "brainhub-core",
        extractor_version: str | None = None,
    ) -> Edge:
        moment = event.time.astimezone(UTC)
        recorded = (self._recorded_at or event.time).astimezone(UTC)
        refs = [evidence] if isinstance(evidence, EvidenceRef) else evidence
        return Edge(
            id=stable_id(
                "edge", event.id, discriminator, source_id, relation.value, target_id
            ),
            source_id=source_id,
            target_id=target_id,
            relation=relation,
            explanation=explanation[:320],
            confidence_class=confidence_class,
            confidence_score=confidence_score,
            evidence=refs,
            valid_time=TimeRange(start=moment),
            recorded_time=TimeRange(start=recorded),
            actor_id=actor_id,
            extractor=extractor,
            extractor_version=extractor_version or self.VERSION,
            sensitivity=sensitivity,
            review_state=review_state,
            properties=properties or {},
            creation_event_id=event.id,
            latest_revision_id=event.id,
        )

    def _explicit_edge(self, event: BrainEvent, item: Any, index: int) -> Edge:
        if not isinstance(item, dict):
            raise ValueError(f"edges[{index}] must be an object")
        if {"valid_time", "recorded_time", "creation_event_id", "evidence"} <= item.keys():
            return Edge.model_validate(item)
        source_id = str(item.get("source_id") or item.get("source") or "")
        target_id = str(item.get("target_id") or item.get("target") or "")
        try:
            relation = EdgeType(str(item.get("relation") or item.get("type") or "").upper())
        except ValueError as exc:
            raise ValueError(f"edges[{index}] has unknown relation") from exc
        if not source_id or not target_id:
            raise ValueError(f"edges[{index}] requires source and target")
        if not item.get("explanation") and not item.get("description"):
            raise ValueError(f"edges[{index}] requires an explanation")
        if item.get("confidence_class") is None and item.get("confidence") is None:
            raise ValueError(f"edges[{index}] requires a confidence class")
        if item.get("confidence_score") is None and item.get("score") is None:
            raise ValueError(f"edges[{index}] requires a confidence score")
        evidence = self._parse_evidence(item.get("evidence"), event.id)
        confidence_class = self._enum_or(
            ConfidenceClass,
            item.get("confidence_class"),
            ConfidenceClass.EXTRACTED,
        )
        edge = self._make_edge(
            event,
            source_id,
            target_id,
            relation,
            str(item.get("explanation") or item.get("description")),
            str(item.get("id") or f"explicit:{index}"),
            evidence=evidence,
            actor_id=item.get("actor_id"),
            confidence_class=confidence_class,
            confidence_score=float(item.get("confidence_score", item.get("score", 1.0))),
            properties=dict(item.get("properties") or {}),
            sensitivity=self._enum_or(Sensitivity, item.get("sensitivity"), Sensitivity.INTERNAL),
            review_state=self._enum_or(ReviewState, item.get("review_state"), ReviewState.UNREVIEWED),
            extractor=str(item.get("extractor") or "brainhub-core"),
            extractor_version=str(item.get("extractor_version") or self.VERSION),
        )
        explicit_id = str(item.get("id") or "")
        if len(explicit_id) >= 3:
            edge = edge.model_copy(update={"id": explicit_id})
        return edge

    def _upsert_node(self, connection: sqlite3.Connection, node: Node) -> Node:
        row = connection.execute(
            "SELECT * FROM nodes WHERE node_id = ?", (node.id,)
        ).fetchone()
        if row is not None:
            previous = self.store._decode_node_row(row)
            node = node.model_copy(
                update={
                    "creation_event_id": previous.creation_event_id,
                    "valid_time": TimeRange(
                        start=min(previous.valid_time.start, node.valid_time.start),
                        end=node.valid_time.end,
                    ),
                }
            )
        self.store.upsert_node(connection, node)
        return node

    def _upsert_edge(self, connection: sqlite3.Connection, edge: Edge) -> Edge:
        row = connection.execute(
            "SELECT * FROM edges WHERE edge_id = ?", (edge.id,)
        ).fetchone()
        if row is not None:
            previous = self.store._decode_edge_row(row)
            edge = edge.model_copy(update={"creation_event_id": previous.creation_event_id})
        self.store.upsert_edge(connection, edge)
        return edge

    @staticmethod
    def _parse_evidence(value: Any, event_id: str) -> list[EvidenceRef]:
        if not value:
            return [EvidenceRef(source_event_id=event_id)]
        if isinstance(value, dict):
            value = [value]
        refs = []
        for item in value:
            if isinstance(item, str):
                refs.append(EvidenceRef(source_event_id=event_id, locator=item))
            else:
                refs.append(
                    EvidenceRef.model_validate(
                        {**dict(item), "source_event_id": event_id}
                    )
                )
        return refs

    @staticmethod
    def _enum_or(enum_type: Any, value: Any, default: Any) -> Any:
        if value is None:
            return default
        try:
            return enum_type(str(value).upper())
        except ValueError:
            return default

    @staticmethod
    def _semantic_explanation(node_type: NodeType, relation: EdgeType) -> str:
        if relation == EdgeType.PRODUCED:
            return "The captured run produced this artifact."
        if relation == EdgeType.DECIDED_IN:
            return "This decision was recorded during the captured run."
        return f"The captured run is about this {node_type.value.lower()}."

    @staticmethod
    def _unique_nodes(nodes: Iterable[Node]) -> list[Node]:
        return list({node.id: node for node in nodes}.values())

    @staticmethod
    def _unique_edges(edges: Iterable[Edge]) -> list[Edge]:
        return list({edge.id: edge for edge in edges}.values())
