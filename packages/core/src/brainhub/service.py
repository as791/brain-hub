"""Application service coordinating policy, persistence, graph, and search."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .graph import EvidenceGraph, GraphNotFoundError
from .graphify import GraphifyImporter
from .models import (
    BrainEvent,
    FeedbackRequest,
    GraphSlice,
    PathResponse,
    NodeType,
    RecordResponse,
    SearchResponse,
    SyncBatch,
)
from .projector import Projector
from .policy import validate_capture_policy
from .search import SearchIndex
from .store import EventStore


class BrainHubService:
    def __init__(
        self,
        store: EventStore,
        *,
        enable_semantic: bool = True,
        allow_raw_content: bool | None = None,
    ) -> None:
        self.store = store
        self.projector = Projector(store)
        self.graph = EvidenceGraph(store)
        self.search_index = SearchIndex(store, enable_semantic=enable_semantic)
        self.graphify = GraphifyImporter()
        self.allow_raw_content = (
            os.environ.get("BRAINHUB_ALLOW_RAW_CONTENT", "").casefold() in {"1", "true", "yes"}
            if allow_raw_content is None
            else allow_raw_content
        )
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    def refresh_graph_if_stale(self) -> int:
        """Refresh a previously loaded graph cache without constructing search state."""

        observed = self.store.projection_version()
        self.graph.refresh_if_loaded(observed)
        return observed

    def record(self, event: BrainEvent | dict[str, Any]) -> RecordResponse:
        parsed = event if isinstance(event, BrainEvent) else BrainEvent.model_validate(event)
        validate_capture_policy(parsed, allow_raw_content=self.allow_raw_content)
        sequence, accepted, projection_version = self.store.append_event(parsed, self.projector)
        if accepted:
            self._publish(
                {
                    "type": "projection.updated",
                    "event_id": parsed.id,
                    "projection_version": projection_version,
                }
            )
        return RecordResponse(
            event_id=parsed.id,
            sequence=sequence,
            accepted=accepted,
            projection_version=projection_version,
        )

    def search(
        self,
        query: str,
        *,
        anchor_id: str | None = None,
        hops: int = 2,
        limit: int = 20,
        global_scope: bool = False,
        valid_at: datetime | None = None,
        node_types: set[NodeType] | None = None,
    ) -> SearchResponse:
        if not global_scope and anchor_id is None:
            raise ValueError("anchored search requires anchor_id")
        if anchor_id is not None and not global_scope:
            distances = self.graph.neighborhood_ids(
                anchor_id, hops=hops, valid_at=valid_at
            )
            return self.search_index.search(
                query,
                limit=limit,
                allowed_ids=set(distances),
                graph_distances=distances,
                scope="anchored",
                anchor_id=anchor_id,
                hops=hops,
                valid_at=valid_at,
                node_types=node_types,
            )
        return self.search_index.search(
            query,
            limit=limit,
            scope="global",
            valid_at=valid_at,
            node_types=node_types,
        )

    def get_node(self, node_id: str):
        return self.store.get_node(node_id)

    def get_graph(
        self,
        *,
        node_limit: int = 2_000,
        edge_limit: int = 10_000,
        valid_at: datetime | None = None,
    ) -> GraphSlice:
        self.refresh_graph_if_stale()
        return self.graph.all(
            node_limit=node_limit, edge_limit=edge_limit, valid_at=valid_at
        )

    def expand(
        self,
        node_id: str,
        *,
        hops: int = 1,
        relation_types: list[str] | None = None,
        node_limit: int = 2_000,
        edge_limit: int = 10_000,
        valid_at: datetime | None = None,
    ) -> GraphSlice:
        self.refresh_graph_if_stale()
        return self.graph.expand(
            node_id,
            hops=hops,
            relation_types=relation_types,
            node_limit=node_limit,
            edge_limit=edge_limit,
            valid_at=valid_at,
        )

    def path(
        self,
        source_id: str,
        target_id: str,
        *,
        directed: bool = False,
        max_length: int = 8,
        valid_at: datetime | None = None,
    ) -> PathResponse:
        self.refresh_graph_if_stale()
        return self.graph.path(
            source_id,
            target_id,
            directed=directed,
            max_length=max_length,
            valid_at=valid_at,
        )

    def feedback(
        self,
        request: FeedbackRequest | dict[str, Any],
        *,
        workspace_id: str = "manual",
        session_id: str = "manual-feedback",
    ) -> RecordResponse:
        parsed = (
            request if isinstance(request, FeedbackRequest) else FeedbackRequest.model_validate(request)
        )
        if (
            self.store.get_node(parsed.target_id) is None
            and self.store.get_edge(parsed.target_id) is None
        ):
            raise GraphNotFoundError(f"feedback target {parsed.target_id!r} was not found")
        event = BrainEvent.create(
            source="urn:brainhub:manual-feedback",
            type="com.brainhub.feedback.recorded.v1",
            subject=f"feedback/{parsed.target_id}",
            time=datetime.now(UTC),
            data={
                "agent": {"product": "brainhub", "surface": "service", "version": "0.1.0"},
                "workspace_id": workspace_id,
                "session_id": session_id,
                "status": "completed",
                "capture": {"mode": "manual", "content_level": "summary"},
                **parsed.model_dump(mode="json", exclude_none=True),
            },
        )
        return self.record(event)

    def import_graphify(
        self,
        path: str | Path,
        *,
        workspace_id: str = "graphify-import",
        workstream_id: str | None = None,
    ) -> RecordResponse:
        event = self.graphify.to_event(
            path, workspace_id=workspace_id, workstream_id=workstream_id
        )
        return self.record(event)

    def next_sync_batch(self, *, limit: int = 500) -> SyncBatch | None:
        return self.store.next_sync_batch(limit=limit)

    def acknowledge_sync(self, last_sequence: int) -> int:
        return self.store.acknowledge_sync(last_sequence)

    def subscribe(self, *, max_queue: int = 100) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=max_queue)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    def _publish(self, message: dict[str, Any]) -> None:
        for queue in tuple(self._subscribers):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                # A slow UI receives the next version and can refresh the snapshot.
                try:
                    queue.get_nowait()
                    queue.put_nowait(message)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

    def close(self) -> None:
        self.store.close()
