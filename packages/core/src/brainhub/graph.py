"""Bounded NetworkX projections for traversal and evidence paths."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
import threading

import networkx as nx

from .models import Edge, GraphSlice, Node, PathResponse
from .store import EventStore


class GraphNotFoundError(KeyError):
    pass


class GraphBoundsError(ValueError):
    pass


class EvidenceGraph:
    MAX_HOPS = 20
    MAX_SCENE_NODES = 2_000
    MAX_SCENE_EDGES = 10_000
    MAX_PATH_LENGTH = 20

    def __init__(self, store: EventStore) -> None:
        self.store = store
        self._cache_lock = threading.RLock()
        self._cache_version = -1
        self._cache: tuple[nx.MultiDiGraph, dict[str, Node], dict[str, Edge]] | None = None

    def _snapshot(self) -> tuple[nx.MultiDiGraph, dict[str, Node], dict[str, Edge]]:
        version = self.store.projection_version()
        with self._cache_lock:
            if self._cache is not None and version == self._cache_version:
                return self._cache
            snapshot_nodes, snapshot_edges, version = self.store.read_graph_snapshot()
            nodes = {node.id: node for node in snapshot_nodes}
            edges = {edge.id: edge for edge in snapshot_edges}
            graph = nx.MultiDiGraph()
            for node in nodes.values():
                graph.add_node(node.id, type=node.type.value)
            for edge in edges.values():
                if edge.source_id in nodes and edge.target_id in nodes:
                    graph.add_edge(
                        edge.source_id,
                        edge.target_id,
                        key=edge.id,
                        edge_id=edge.id,
                        relation=edge.relation.value,
                    )
            self._cache_version = version
            self._cache = (graph, nodes, edges)
            return self._cache

    @staticmethod
    def _active(start: datetime, end: datetime | None, valid_at: datetime) -> bool:
        return start <= valid_at and (end is None or valid_at <= end)

    def _snapshot_at(
        self, valid_at: datetime | None
    ) -> tuple[nx.MultiDiGraph, dict[str, Node], dict[str, Edge]]:
        graph, nodes, edges = self._snapshot()
        if valid_at is None:
            return graph, nodes, edges
        active_nodes = {
            node_id: node
            for node_id, node in nodes.items()
            if self._active(node.valid_time.start, node.valid_time.end, valid_at)
        }
        active_edges = {
            edge_id: edge
            for edge_id, edge in edges.items()
            if edge.source_id in active_nodes
            and edge.target_id in active_nodes
            and self._active(edge.valid_time.start, edge.valid_time.end, valid_at)
        }
        filtered = nx.MultiDiGraph()
        for node in active_nodes.values():
            filtered.add_node(node.id, type=node.type.value)
        for edge in active_edges.values():
            filtered.add_edge(
                edge.source_id,
                edge.target_id,
                key=edge.id,
                edge_id=edge.id,
                relation=edge.relation.value,
            )
        return filtered, active_nodes, active_edges

    @property
    def cache_version(self) -> int:
        with self._cache_lock:
            return self._cache_version

    def refresh_if_loaded(self, expected_version: int) -> None:
        with self._cache_lock:
            stale = self._cache is not None and self._cache_version != expected_version
        if stale:
            self._snapshot()

    def all(
        self,
        *,
        node_limit: int = 2_000,
        edge_limit: int = 10_000,
        valid_at: datetime | None = None,
    ) -> GraphSlice:
        node_limit = max(1, min(node_limit, self.MAX_SCENE_NODES))
        edge_limit = max(0, min(edge_limit, self.MAX_SCENE_EDGES))
        graph, nodes, edges = self._snapshot_at(valid_at)
        selected_nodes = list(nodes.values())[:node_limit]
        selected_ids = {node.id for node in selected_nodes}
        selected_edges = [
            edge
            for edge in edges.values()
            if edge.source_id in selected_ids and edge.target_id in selected_ids
        ][:edge_limit]
        return GraphSlice(
            nodes=selected_nodes,
            edges=selected_edges,
            projection_version=self.cache_version,
        )

    def neighborhood_ids(
        self, anchor_id: str, *, hops: int = 2, valid_at: datetime | None = None
    ) -> dict[str, int]:
        if not 0 <= hops <= self.MAX_HOPS:
            raise GraphBoundsError(f"hops must be between 0 and {self.MAX_HOPS}")
        graph, _, _ = self._snapshot_at(valid_at)
        if anchor_id not in graph:
            raise GraphNotFoundError(anchor_id)
        undirected = graph.to_undirected(as_view=True)
        lengths = nx.single_source_shortest_path_length(undirected, anchor_id, cutoff=hops)
        return dict(lengths)

    def expand(
        self,
        anchor_id: str,
        *,
        hops: int = 1,
        relation_types: Iterable[str] | None = None,
        node_limit: int = 2_000,
        edge_limit: int = 10_000,
        valid_at: datetime | None = None,
    ) -> GraphSlice:
        node_limit = max(1, min(node_limit, self.MAX_SCENE_NODES))
        edge_limit = max(0, min(edge_limit, self.MAX_SCENE_EDGES))
        distances = self.neighborhood_ids(anchor_id, hops=hops, valid_at=valid_at)
        graph, nodes, edges = self._snapshot_at(valid_at)
        del graph
        ordered_ids = sorted(distances, key=lambda node_id: (distances[node_id], node_id))[
            :node_limit
        ]
        selected = set(ordered_ids)
        relations = {value.upper() for value in relation_types or []}
        selected_edges = [
            edge
            for edge in edges.values()
            if edge.source_id in selected
            and edge.target_id in selected
            and (not relations or edge.relation.value in relations)
        ][:edge_limit]
        return GraphSlice(
            nodes=[nodes[node_id] for node_id in ordered_ids],
            edges=selected_edges,
            anchor_id=anchor_id,
            hops=hops,
            projection_version=self.cache_version,
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
        if not 1 <= max_length <= self.MAX_PATH_LENGTH:
            raise GraphBoundsError(f"max_length must be between 1 and {self.MAX_PATH_LENGTH}")
        graph, nodes, edges = self._snapshot_at(valid_at)
        if source_id not in graph:
            raise GraphNotFoundError(source_id)
        if target_id not in graph:
            raise GraphNotFoundError(target_id)
        search_graph = graph if directed else graph.to_undirected(as_view=True)
        try:
            node_path = nx.shortest_path(search_graph, source_id, target_id)
        except nx.NetworkXNoPath as exc:
            raise GraphNotFoundError(f"no evidence path from {source_id} to {target_id}") from exc
        if len(node_path) - 1 > max_length:
            raise GraphBoundsError(
                f"shortest path has {len(node_path) - 1} edges; limit is {max_length}"
            )

        path_edges: list[Edge] = []
        for left, right in zip(node_path, node_path[1:], strict=False):
            candidates = [
                edge
                for edge in edges.values()
                if (edge.source_id == left and edge.target_id == right)
                or (not directed and edge.source_id == right and edge.target_id == left)
            ]
            if not candidates:  # pragma: no cover - graph built from the same edge snapshot
                raise GraphNotFoundError(f"path edge disappeared between {left} and {right}")
            candidates.sort(
                key=lambda edge: (
                    edge.confidence_class.value != "EXTRACTED",
                    -edge.confidence_score,
                    edge.id,
                )
            )
            path_edges.append(candidates[0])
        return PathResponse(
            nodes=[nodes[node_id] for node_id in node_path],
            edges=path_edges,
            projection_version=self.cache_version,
        )
