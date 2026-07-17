"""Ephemeral Semble hybrid search with an explicit lexical degradation path."""

from __future__ import annotations

import math
import os
import re
import tempfile
import threading
from collections import Counter
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from .models import Node, NodeType, SearchResponse, SearchResult
from .redaction import redact_text
from .store import EventStore


TOKEN_RE = re.compile(r"[\w-]+", re.UNICODE)
_SEMBLE_BUILD_LOCK = threading.Lock()


def redact_for_index(text: str) -> str:
    return redact_text(text)


@contextmanager
def _temporary_semble_cache(path: str) -> Iterator[None]:
    previous = os.environ.get("SEMBLE_CACHE_LOCATION")
    os.environ["SEMBLE_CACHE_LOCATION"] = path
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("SEMBLE_CACHE_LOCATION", None)
        else:
            os.environ["SEMBLE_CACHE_LOCATION"] = previous


class SearchIndex:
    """A process-memory search snapshot; no Semble persistence method is called."""

    def __init__(self, store: EventStore, *, enable_semantic: bool = True) -> None:
        self.store = store
        self.enable_semantic = enable_semantic
        self._lock = threading.RLock()
        self._rebuild_lock = threading.Lock()
        self._projection_version = -1
        self._nodes: dict[str, Node] = {}
        self._documents: dict[str, str] = {}
        self._token_counts: dict[str, Counter[str]] = {}
        self._document_frequency: Counter[str] = Counter()
        self._semble: Any | None = None
        self._semble_paths: dict[str, str] = {}
        self._degraded_reason: str | None = "semantic index has not been built"

    @property
    def degraded_reason(self) -> str | None:
        with self._lock:
            return self._degraded_reason

    @property
    def projection_version(self) -> int:
        with self._lock:
            return self._projection_version

    def ensure_current(self) -> int:
        current = self.store.projection_version()
        if self.projection_version != current:
            self.rebuild()
        return self.projection_version

    def rebuild(self) -> None:
        with self._rebuild_lock:
            self._rebuild_snapshot()

    def _rebuild_snapshot(self, attempt: int = 0) -> None:
        source_version = self.store.projection_version()
        nodes = self.store.list_nodes()
        documents: dict[str, str] = {}
        token_counts: dict[str, Counter[str]] = {}
        document_frequency: Counter[str] = Counter()
        for node in nodes:
            content = " ".join(
                part
                for part in (
                    node.title,
                    node.summary,
                    " ".join(str(value) for value in node.properties.values()),
                )
                if part
            )
            redacted = redact_for_index(content)
            documents[node.id] = redacted
            counts = Counter(self._tokens(redacted))
            token_counts[node.id] = counts
            document_frequency.update(counts.keys())

        semble_index: Any | None = None
        semble_paths: dict[str, str] = {}
        degraded_reason: str | None = None
        if self.enable_semantic and nodes:
            try:
                from semble import ContentType, SembleIndex

                with tempfile.TemporaryDirectory(prefix="brainhub-search-") as tmp:
                    root = Path(tmp)
                    docs = root / "docs"
                    cache = root / "cache"
                    docs.mkdir()
                    cache.mkdir()
                    for ordinal, node in enumerate(nodes):
                        filename = f"node-{ordinal:08d}.md"
                        path = docs / filename
                        path.write_text(
                            f"# {redact_for_index(node.title)}\n\n"
                            f"{documents[node.id]}\n",
                            encoding="utf-8",
                        )
                        semble_paths[filename] = node.id
                        semble_paths[str(path)] = node.id
                    with _SEMBLE_BUILD_LOCK, _temporary_semble_cache(str(cache)):
                        # from_path eagerly builds an in-memory index. We deliberately
                        # never call save(); the redacted source and cache are destroyed.
                        semble_index = SembleIndex.from_path(docs, content=ContentType.DOCS)
            except Exception as exc:  # dependency/model/backend failures are transparent
                degraded_reason = f"Semble unavailable: {type(exc).__name__}: {exc}"
        elif not self.enable_semantic:
            degraded_reason = "semantic search disabled by configuration"
        elif not nodes:
            degraded_reason = "graph has no searchable nodes"

        if self.store.projection_version() != source_version and attempt < 2:
            self._rebuild_snapshot(attempt + 1)
            return

        with self._lock:
            self._nodes = {node.id: node for node in nodes}
            self._documents = documents
            self._token_counts = token_counts
            self._document_frequency = document_frequency
            self._semble = semble_index
            self._semble_paths = semble_paths
            self._degraded_reason = degraded_reason if semble_index is None else None
            self._projection_version = source_version

    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        allowed_ids: set[str] | None = None,
        graph_distances: dict[str, int] | None = None,
        scope: str = "global",
        anchor_id: str | None = None,
        hops: int | None = None,
        valid_at: datetime | None = None,
        node_types: set[NodeType] | None = None,
    ) -> SearchResponse:
        self.ensure_current()
        query = query.strip()
        if not query:
            raise ValueError("query must not be empty")
        limit = max(1, min(limit, 100))
        with self._lock:
            nodes = dict(self._nodes)
            token_counts = dict(self._token_counts)
            document_frequency = self._document_frequency.copy()
            semble_index = self._semble
            semble_paths = dict(self._semble_paths)
            degraded_reason = self._degraded_reason
            projection_version = self._projection_version

        candidate_ids = set(nodes)
        if allowed_ids is not None:
            candidate_ids &= allowed_ids
        if node_types:
            candidate_ids = {
                node_id for node_id in candidate_ids if nodes[node_id].type in node_types
            }
        if valid_at is not None:
            candidate_ids = {
                node_id
                for node_id in candidate_ids
                if nodes[node_id].valid_time.start <= valid_at
                and (
                    nodes[node_id].valid_time.end is None
                    or valid_at <= nodes[node_id].valid_time.end
                )
            }
        query_tokens = self._tokens(query)
        lexical = self._lexical_scores(
            query_tokens, candidate_ids, token_counts, document_frequency, len(nodes)
        )
        semantic: dict[str, float] = {}
        if semble_index is not None and candidate_ids:
            try:
                raw_results = semble_index.search(
                    query,
                    top_k=min(max(limit * 8, len(candidate_ids)), max(len(nodes), 1)),
                )
                for result in raw_results:
                    file_path = str(result.chunk.file_path)
                    node_id = (
                        semble_paths.get(file_path)
                        or semble_paths.get(Path(file_path).name)
                    )
                    if node_id in candidate_ids:
                        semantic[node_id] = max(semantic.get(node_id, 0.0), float(result.score))
            except Exception as exc:
                semble_index = None
                degraded_reason = f"Semble query failed: {type(exc).__name__}: {exc}"
        semantic = self._normalize(semantic)
        lexical = self._normalize(lexical)

        ranked: list[SearchResult] = []
        for node_id in candidate_ids:
            lexical_score = lexical.get(node_id, 0.0)
            semantic_score = semantic.get(node_id) if semble_index is not None else None
            has_text_match = (
                lexical_score > 0
                or (semble_index is not None and (semantic_score or 0.0) > 0)
            )
            if not has_text_match:
                continue
            distance = (graph_distances or {}).get(node_id)
            graph_score = 1 / (1 + distance) if distance is not None else 0.0
            if semble_index is not None:
                score = 0.45 * lexical_score + 0.45 * (semantic_score or 0.0) + 0.10 * graph_score
            else:
                score = 0.90 * lexical_score + 0.10 * graph_score
            if score > 0:
                ranked.append(
                    SearchResult(
                        node=nodes[node_id],
                        score=score,
                        lexical_score=lexical_score,
                        semantic_score=semantic_score,
                        graph_score=graph_score,
                    )
                )
        ranked.sort(key=lambda result: (-result.score, result.node.id))
        return SearchResponse(
            results=ranked[:limit],
            search_mode="hybrid" if semble_index is not None else "lexical_degraded",
            scope="anchored" if scope == "anchored" else "global",
            anchor_id=anchor_id,
            hops=hops,
            projection_version=projection_version,
            degraded_reason=degraded_reason if semble_index is None else None,
        )

    @staticmethod
    def _tokens(text: str) -> list[str]:
        return [token.casefold() for token in TOKEN_RE.findall(text)]

    @staticmethod
    def _lexical_scores(
        query_tokens: list[str],
        candidates: set[str],
        counts: dict[str, Counter[str]],
        document_frequency: Counter[str],
        document_count: int,
    ) -> dict[str, float]:
        scores: dict[str, float] = {}
        for node_id in candidates:
            terms = counts.get(node_id, Counter())
            length = max(sum(terms.values()), 1)
            score = 0.0
            for token in query_tokens:
                frequency = terms.get(token, 0)
                if not frequency:
                    continue
                inverse_frequency = math.log(
                    1 + (document_count + 1) / (document_frequency.get(token, 0) + 1)
                )
                score += (1 + math.log(frequency)) * inverse_frequency / math.sqrt(length)
            if score:
                scores[node_id] = score
        return scores

    @staticmethod
    def _normalize(values: dict[str, float]) -> dict[str, float]:
        if not values:
            return {}
        low = min(values.values())
        high = max(values.values())
        if high == low:
            return {key: 1.0 if high > 0 else 0.0 for key in values}
        return {key: (value - low) / (high - low) for key, value in values.items()}
