"""Encrypted append-only SQLite event store and rebuildable graph projection."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

from pydantic import ValidationError

from .crypto import ContentCipher
from .models import (
    BrainEvent,
    Edge,
    Node,
    SyncBatch,
    SyncEvent,
    canonical_json,
    sha256_hex,
)
from .redaction import opaque_identifier, sanitize_sync_graph_payload

if TYPE_CHECKING:
    from .projector import Projector


class EventIntegrityError(RuntimeError):
    """An event ID was reused with different canonical content."""


class ProjectionIntegrityError(RuntimeError):
    pass


class DemoResetRefused(RuntimeError):
    pass


class EventStore:
    """SQLite is the solo installation's authority; derived tables are replayable."""

    SCHEMA_VERSION = 1
    NODE_INDEX_CHECKPOINT_PENDING = "node_index_checkpoint_pending"
    SYNC_CHECKPOINT_PENDING = "sync_checkpoint_pending"

    def __init__(self, path: str | Path, cipher: ContentCipher) -> None:
        self.path = str(path)
        self.cipher = cipher
        self._lock = threading.RLock()
        if self.path != ":memory:":
            parent = Path(self.path).expanduser().resolve().parent
            parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.path = str(Path(self.path).expanduser())
        self.connection = sqlite3.connect(
            self.path,
            isolation_level=None,
            check_same_thread=False,
            timeout=10,
        )
        self.connection.row_factory = sqlite3.Row
        self._configure()
        self._migrate()
        self._migrate_node_indexes()
        self._migrate_sync_payloads()
        if self.path != ":memory:":
            os.chmod(self.path, 0o600)

    def _configure(self) -> None:
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA secure_delete=ON")
        self.connection.execute("PRAGMA busy_timeout=10000")

    def _migrate(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                canonical_sha256 TEXT NOT NULL,
                source TEXT NOT NULL,
                event_type TEXT NOT NULL,
                subject TEXT NOT NULL,
                event_time TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                encrypted_payload BLOB NOT NULL,
                projection_status TEXT NOT NULL DEFAULT 'pending'
            );
            CREATE INDEX IF NOT EXISTS events_type_sequence_idx
                ON events(event_type, sequence);

            CREATE TABLE IF NOT EXISTS nodes (
                node_id TEXT PRIMARY KEY,
                node_type TEXT NOT NULL,
                sensitivity TEXT NOT NULL,
                review_state TEXT NOT NULL,
                valid_from TEXT NOT NULL,
                valid_to TEXT,
                recorded_at TEXT NOT NULL,
                external_ids TEXT NOT NULL,
                content_hash TEXT,
                creation_event_id TEXT NOT NULL,
                latest_revision_id TEXT NOT NULL,
                encrypted_payload BLOB NOT NULL
            );
            CREATE INDEX IF NOT EXISTS nodes_type_idx ON nodes(node_type);

            CREATE TABLE IF NOT EXISTS edges (
                edge_id TEXT PRIMARY KEY,
                source_id TEXT NOT NULL,
                target_id TEXT NOT NULL,
                relation TEXT NOT NULL,
                sensitivity TEXT NOT NULL,
                review_state TEXT NOT NULL,
                valid_from TEXT NOT NULL,
                valid_to TEXT,
                recorded_at TEXT NOT NULL,
                creation_event_id TEXT NOT NULL,
                latest_revision_id TEXT NOT NULL,
                encrypted_payload BLOB NOT NULL,
                FOREIGN KEY(source_id) REFERENCES nodes(node_id),
                FOREIGN KEY(target_id) REFERENCES nodes(node_id)
            );
            CREATE INDEX IF NOT EXISTS edges_source_idx ON edges(source_id, relation);
            CREATE INDEX IF NOT EXISTS edges_target_idx ON edges(target_id, relation);

            CREATE TABLE IF NOT EXISTS projector_checkpoint (
                projector TEXT PRIMARY KEY,
                version TEXT NOT NULL,
                last_sequence INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sync_spool (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                event_type TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                canonical_sha256 TEXT NOT NULL,
                graph_payload TEXT NOT NULL,
                acknowledged_at TEXT
            );
            CREATE INDEX IF NOT EXISTS sync_pending_idx
                ON sync_spool(acknowledged_at, sequence);

            CREATE TABLE IF NOT EXISTS sync_issued_batches (
                batch_id TEXT PRIMARY KEY,
                first_sequence INTEGER NOT NULL,
                last_sequence INTEGER NOT NULL,
                issued_at TEXT NOT NULL,
                acknowledged_at TEXT
            );
            """
        )
        self.connection.execute(
            "INSERT OR IGNORE INTO metadata(key, value) VALUES ('schema_version', ?)",
            (str(self.SCHEMA_VERSION),),
        )
        self.connection.execute(
            "INSERT OR IGNORE INTO metadata(key, value) VALUES ('installation_id', ?)",
            (str(uuid.uuid4()),),
        )

    def _migrate_sync_payloads(self) -> None:
        """Encrypt legacy plaintext sync rows and bind their hash to graph-only data."""

        self._finish_pending_checkpoint(
            self.SYNC_CHECKPOINT_PENDING,
            "legacy plaintext sync WAL",
        )
        boundary = self.connection.execute(
            "SELECT value FROM metadata WHERE key = 'sync_boundary_version'"
        ).fetchone()
        boundary_version = int(boundary["value"]) if boundary is not None else 0
        if boundary_version >= 3:
            payloads = self.connection.execute(
                "SELECT graph_payload FROM sync_spool"
            ).fetchall()
            if all(
                isinstance(row["graph_payload"], bytes)
                and row["graph_payload"].startswith(ContentCipher.VERSION)
                for row in payloads
            ):
                return
        migrated = False
        with self.transaction() as connection:
            rows = connection.execute(
                "SELECT sequence, event_id, graph_payload FROM sync_spool"
            ).fetchall()
            for row in rows:
                stored = row["graph_payload"]
                if isinstance(stored, memoryview):
                    stored = stored.tobytes()
                old_event_id = str(row["event_id"])
                if isinstance(stored, bytes) and stored.startswith(ContentCipher.VERSION):
                    raw = self.cipher.decrypt(
                        stored, context=f"sync:{old_event_id}".encode()
                    )
                else:
                    raw = stored if isinstance(stored, bytes) else str(stored).encode("utf-8")
                try:
                    graph_payload = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    raise ProjectionIntegrityError(
                        f"legacy sync row {row['sequence']} contains invalid graph JSON"
                    ) from exc
                if boundary_version < 3:
                    graph_payload = sanitize_sync_graph_payload(
                        graph_payload, self.cipher.pseudonymize
                    )
                    new_event_id = opaque_identifier(
                        "event", old_event_id, self.cipher.pseudonymize
                    )
                else:
                    new_event_id = old_event_id
                canonical = canonical_json(graph_payload)
                encrypted = self.cipher.encrypt(
                    canonical, context=f"sync:{new_event_id}".encode()
                )
                connection.execute(
                    """
                    UPDATE sync_spool
                    SET event_id = ?, graph_payload = ?, canonical_sha256 = ?
                    WHERE sequence = ?
                    """,
                    (
                        new_event_id,
                        encrypted,
                        sha256_hex(graph_payload),
                        row["sequence"],
                    ),
                )
                migrated = True
            if migrated:
                connection.execute(
                    """
                    INSERT INTO metadata(key, value) VALUES (?, '1')
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (self.SYNC_CHECKPOINT_PENDING,),
                )
            connection.execute(
                """
                INSERT INTO metadata(key, value) VALUES ('sync_boundary_version', '3')
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """
            )
        if migrated:
            self._finish_pending_checkpoint(
                self.SYNC_CHECKPOINT_PENDING,
                "legacy plaintext sync WAL",
            )

    def _external_index_values(self, values: list[str]) -> list[str]:
        return [
            "external-index:"
            + self.cipher.pseudonymize("local-external-index", str(value))
            for value in values
        ]

    def _migrate_node_indexes(self) -> None:
        """Replace legacy plaintext external-ID lookup values with keyed indexes."""

        self._finish_pending_checkpoint(
            self.NODE_INDEX_CHECKPOINT_PENDING,
            "legacy plaintext node-index WAL",
        )
        boundary = self.connection.execute(
            "SELECT value FROM metadata WHERE key = 'node_index_boundary_version'"
        ).fetchone()
        if boundary is not None and int(boundary["value"]) >= 1:
            return
        migrated = False
        with self.transaction() as connection:
            rows = connection.execute("SELECT * FROM nodes").fetchall()
            for row in rows:
                node = self._decode_node_row(row)
                connection.execute(
                    "UPDATE nodes SET external_ids = ? WHERE node_id = ?",
                    (
                        json.dumps(
                            self._external_index_values(node.external_ids),
                            sort_keys=True,
                        ),
                        node.id,
                    ),
                )
                migrated = True
            if migrated:
                connection.execute(
                    """
                    INSERT INTO metadata(key, value) VALUES (?, '1')
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (self.NODE_INDEX_CHECKPOINT_PENDING,),
                )
            connection.execute(
                """
                INSERT INTO metadata(key, value) VALUES ('node_index_boundary_version', '1')
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """
            )
        if migrated:
            self._finish_pending_checkpoint(
                self.NODE_INDEX_CHECKPOINT_PENDING,
                "legacy plaintext node-index WAL",
            )

    def _truncate_wal(self):
        return self.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()

    def _finish_pending_checkpoint(self, marker: str, description: str) -> bool:
        pending = self.connection.execute(
            "SELECT 1 FROM metadata WHERE key = ? AND value = '1'",
            (marker,),
        ).fetchone()
        if pending is None:
            return False
        checkpoint = self._truncate_wal()
        if checkpoint is not None and int(checkpoint[0]) != 0:
            raise ProjectionIntegrityError(
                f"could not truncate {description}; retry with exclusive access"
            )
        self.connection.execute("DELETE FROM metadata WHERE key = ?", (marker,))
        return True

    @property
    def installation_id(self) -> str:
        row = self.connection.execute(
            "SELECT value FROM metadata WHERE key = 'installation_id'"
        ).fetchone()
        if row is None:  # pragma: no cover - guaranteed by migration
            raise RuntimeError("installation ID is missing")
        return str(row["value"])

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield self.connection
            except BaseException:
                self.connection.execute("ROLLBACK")
                raise
            else:
                self.connection.execute("COMMIT")

    def close(self) -> None:
        with self._lock:
            self.connection.close()

    def append_event(
        self, event: BrainEvent, projector: "Projector"
    ) -> tuple[int, bool, int]:
        canonical = canonical_json(event)
        digest = sha256_hex(event)
        recorded_at = datetime.now(UTC)
        now = recorded_at.isoformat()
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT sequence, canonical_sha256 FROM events WHERE event_id = ?",
                (event.id,),
            ).fetchone()
            if existing is not None:
                if existing["canonical_sha256"] != digest:
                    raise EventIntegrityError(
                        f"event ID {event.id!r} is already bound to different content"
                    )
                return int(existing["sequence"]), False, self.projection_version(connection)

            encrypted = self.cipher.encrypt(canonical, context=f"event:{event.id}".encode())
            cursor = connection.execute(
                """
                INSERT INTO events(
                    event_id, canonical_sha256, source, event_type, subject,
                    event_time, recorded_at, encrypted_payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.id,
                    digest,
                    event.source,
                    event.type,
                    event.subject,
                    event.time.isoformat(),
                    now,
                    encrypted,
                ),
            )
            sequence = int(cursor.lastrowid)
            try:
                graph_payload = projector.project(
                    connection, event, sequence, recorded_at=recorded_at
                )
            except ProjectionIntegrityError:
                raise
            except (ValidationError, ValueError, KeyError, TypeError) as exc:
                raise ProjectionIntegrityError(f"invalid graph facts: {exc}") from exc
            graph_payload = sanitize_sync_graph_payload(
                graph_payload, self.cipher.pseudonymize
            )
            sync_event_id = opaque_identifier(
                "event", event.id, self.cipher.pseudonymize
            )
            canonical_graph_payload = canonical_json(graph_payload)
            encrypted_graph_payload = self.cipher.encrypt(
                canonical_graph_payload, context=f"sync:{sync_event_id}".encode()
            )
            connection.execute(
                "UPDATE events SET projection_status = 'projected' WHERE sequence = ?",
                (sequence,),
            )
            connection.execute(
                """
                INSERT INTO projector_checkpoint(projector, version, last_sequence, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(projector) DO UPDATE SET
                    version = excluded.version,
                    last_sequence = excluded.last_sequence,
                    updated_at = excluded.updated_at
                """,
                (projector.NAME, projector.VERSION, sequence, now),
            )
            connection.execute(
                """
                INSERT INTO sync_spool(
                    event_id, event_type, recorded_at, canonical_sha256, graph_payload
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    sync_event_id,
                    event.type,
                    now,
                    sha256_hex(graph_payload),
                    encrypted_graph_payload,
                ),
            )
            return sequence, True, sequence

    def get_event(self, event_id: str) -> BrainEvent | None:
        with self._lock:
            row = self.connection.execute(
                "SELECT encrypted_payload FROM events WHERE event_id = ?", (event_id,)
            ).fetchone()
        if row is None:
            return None
        plaintext = self.cipher.decrypt(
            row["encrypted_payload"], context=f"event:{event_id}".encode()
        )
        return BrainEvent.model_validate_json(plaintext)

    def projection_version(self, connection: sqlite3.Connection | None = None) -> int:
        if connection is None:
            with self._lock:
                row = self.connection.execute(
                    "SELECT COALESCE(MAX(last_sequence), 0) AS v FROM projector_checkpoint"
                ).fetchone()
        else:
            row = connection.execute(
                "SELECT COALESCE(MAX(last_sequence), 0) AS v FROM projector_checkpoint"
            ).fetchone()
        return int(row["v"] if row is not None else 0)

    def upsert_node(self, connection: sqlite3.Connection, node: Node) -> None:
        payload = self.cipher.encrypt(
            canonical_json(node), context=f"node:{node.id}".encode()
        )
        connection.execute(
            """
            INSERT INTO nodes(
                node_id, node_type, sensitivity, review_state, valid_from, valid_to,
                recorded_at, external_ids, content_hash, creation_event_id,
                latest_revision_id, encrypted_payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(node_id) DO UPDATE SET
                node_type = excluded.node_type,
                sensitivity = excluded.sensitivity,
                review_state = excluded.review_state,
                valid_from = excluded.valid_from,
                valid_to = excluded.valid_to,
                recorded_at = excluded.recorded_at,
                external_ids = excluded.external_ids,
                content_hash = excluded.content_hash,
                latest_revision_id = excluded.latest_revision_id,
                encrypted_payload = excluded.encrypted_payload
            """,
            (
                node.id,
                node.type.value,
                node.sensitivity.value,
                node.review_state.value,
                node.valid_time.start.isoformat(),
                node.valid_time.end.isoformat() if node.valid_time.end else None,
                node.recorded_time.start.isoformat(),
                json.dumps(self._external_index_values(node.external_ids), sort_keys=True),
                node.content_hash,
                node.creation_event_id,
                node.latest_revision_id,
                payload,
            ),
        )

    def upsert_edge(self, connection: sqlite3.Connection, edge: Edge) -> None:
        missing = [
            node_id
            for node_id in (edge.source_id, edge.target_id)
            if connection.execute(
                "SELECT 1 FROM nodes WHERE node_id = ?", (node_id,)
            ).fetchone()
            is None
        ]
        if missing:
            raise ProjectionIntegrityError(f"edge {edge.id!r} has missing nodes: {missing}")
        payload = self.cipher.encrypt(
            canonical_json(edge), context=f"edge:{edge.id}".encode()
        )
        connection.execute(
            """
            INSERT INTO edges(
                edge_id, source_id, target_id, relation, sensitivity, review_state,
                valid_from, valid_to, recorded_at, creation_event_id,
                latest_revision_id, encrypted_payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(edge_id) DO UPDATE SET
                source_id = excluded.source_id,
                target_id = excluded.target_id,
                relation = excluded.relation,
                sensitivity = excluded.sensitivity,
                review_state = excluded.review_state,
                valid_from = excluded.valid_from,
                valid_to = excluded.valid_to,
                recorded_at = excluded.recorded_at,
                latest_revision_id = excluded.latest_revision_id,
                encrypted_payload = excluded.encrypted_payload
            """,
            (
                edge.id,
                edge.source_id,
                edge.target_id,
                edge.relation.value,
                edge.sensitivity.value,
                edge.review_state.value,
                edge.valid_time.start.isoformat(),
                edge.valid_time.end.isoformat() if edge.valid_time.end else None,
                edge.recorded_time.start.isoformat(),
                edge.creation_event_id,
                edge.latest_revision_id,
                payload,
            ),
        )

    def _decode_node_row(self, row: sqlite3.Row) -> Node:
        plaintext = self.cipher.decrypt(
            row["encrypted_payload"], context=f"node:{row['node_id']}".encode()
        )
        return Node.model_validate_json(plaintext)

    def _decode_edge_row(self, row: sqlite3.Row) -> Edge:
        plaintext = self.cipher.decrypt(
            row["encrypted_payload"], context=f"edge:{row['edge_id']}".encode()
        )
        return Edge.model_validate_json(plaintext)

    def get_node(self, node_id: str) -> Node | None:
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM nodes WHERE node_id = ?", (node_id,)
            ).fetchone()
        return self._decode_node_row(row) if row is not None else None

    def get_edge(self, edge_id: str) -> Edge | None:
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM edges WHERE edge_id = ?", (edge_id,)
            ).fetchone()
        return self._decode_edge_row(row) if row is not None else None

    def list_nodes(self, *, limit: int = 100_000) -> list[Node]:
        bounded = max(0, min(limit, 100_000))
        with self._lock:
            rows = self.connection.execute(
                "SELECT * FROM nodes ORDER BY recorded_at, node_id LIMIT ?", (bounded,)
            ).fetchall()
        return [self._decode_node_row(row) for row in rows]

    def list_edges(self, *, limit: int = 1_000_000) -> list[Edge]:
        bounded = max(0, min(limit, 1_000_000))
        with self._lock:
            rows = self.connection.execute(
                "SELECT * FROM edges ORDER BY recorded_at, edge_id LIMIT ?", (bounded,)
            ).fetchall()
        edges = [self._decode_edge_row(row) for row in rows]
        return [
            edge
            for edge in edges
            if any(reference.visibility.value != "UNAVAILABLE" for reference in edge.evidence)
        ]

    def read_graph_snapshot(self) -> tuple[list[Node], list[Edge], int]:
        """Read nodes, edges, and checkpoint from one cross-process SQLite snapshot."""

        with self._lock:
            self.connection.execute("BEGIN")
            try:
                node_rows = self.connection.execute(
                    "SELECT * FROM nodes ORDER BY recorded_at, node_id LIMIT 100000"
                ).fetchall()
                edge_rows = self.connection.execute(
                    "SELECT * FROM edges ORDER BY recorded_at, edge_id LIMIT 1000000"
                ).fetchall()
                version = self.projection_version(self.connection)
            except BaseException:
                self.connection.execute("ROLLBACK")
                raise
            else:
                self.connection.execute("COMMIT")
        nodes = [self._decode_node_row(row) for row in node_rows]
        edges = [self._decode_edge_row(row) for row in edge_rows]
        visible_edges = [
            edge
            for edge in edges
            if any(reference.visibility.value != "UNAVAILABLE" for reference in edge.evidence)
        ]
        return nodes, visible_edges, version

    def next_sync_batch(self, *, limit: int = 500) -> SyncBatch | None:
        bounded = max(1, min(limit, 500))
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT * FROM sync_spool
                WHERE acknowledged_at IS NULL
                ORDER BY sequence
                LIMIT ?
                """,
                (bounded,),
            ).fetchall()
        if not rows:
            return None
        events: list[SyncEvent] = []
        for row in rows:
            graph_payload = json.loads(
                self.cipher.decrypt(
                    bytes(row["graph_payload"]),
                    context=f"sync:{row['event_id']}".encode(),
                )
            )
            events.append(
                SyncEvent(
                    sequence=row["sequence"],
                    event_id=row["event_id"],
                    event_type=row["event_type"],
                    recorded_at=row["recorded_at"],
                    canonical_sha256=sha256_hex(graph_payload),
                    graph_payload=graph_payload,
                )
            )
        first, last = events[0].sequence, events[-1].sequence
        batch_id = str(uuid.uuid5(uuid.UUID(self.installation_id), f"{first}:{last}"))
        batch = SyncBatch(
            installation_id=self.installation_id,
            batch_id=batch_id,
            first_sequence=first,
            last_sequence=last,
            events=events,
        )
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO sync_issued_batches(
                    batch_id, first_sequence, last_sequence, issued_at
                ) VALUES (?, ?, ?, ?)
                """,
                (batch_id, first, last, datetime.now(UTC).isoformat()),
            )
        return batch

    def acknowledge_sync(self, last_sequence: int) -> int:
        now = datetime.now(UTC).isoformat()
        with self.transaction() as connection:
            issued = connection.execute(
                """
                SELECT batch_id, first_sequence, last_sequence
                FROM sync_issued_batches
                WHERE last_sequence = ? AND acknowledged_at IS NULL
                ORDER BY issued_at DESC LIMIT 1
                """,
                (last_sequence,),
            ).fetchone()
            if issued is None:
                raise ValueError("sync acknowledgement does not match an issued pending batch")
            cursor = connection.execute(
                """
                UPDATE sync_spool SET acknowledged_at = ?
                WHERE sequence BETWEEN ? AND ? AND acknowledged_at IS NULL
                """,
                (now, issued["first_sequence"], issued["last_sequence"]),
            )
            connection.execute(
                "UPDATE sync_issued_batches SET acknowledged_at = ? WHERE batch_id = ?",
                (now, issued["batch_id"]),
            )
            return int(cursor.rowcount)

    def reset_if_only_events(self, allowed_event_ids: set[str]) -> None:
        """Reset an isolated fixture DB; refuse to erase personal graph history."""

        with self.transaction() as connection:
            existing = {
                str(row["event_id"])
                for row in connection.execute("SELECT event_id FROM events").fetchall()
            }
            unexpected = existing - allowed_event_ids
            if unexpected:
                raise DemoResetRefused(
                    "refusing demo reset because the database contains non-demo events; "
                    "set BRAINHUB_DB_PATH to a dedicated evaluation database"
                )
            for table in (
                "edges",
                "nodes",
                "sync_issued_batches",
                "sync_spool",
                "projector_checkpoint",
                "events",
            ):
                connection.execute(f"DELETE FROM {table}")
            connection.execute(
                "DELETE FROM sqlite_sequence WHERE name IN ('events', 'sync_spool')"
            )

    def counts(self) -> dict[str, int]:
        with self._lock:
            return {
                table: int(
                    self.connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
                )
                for table in ("events", "nodes", "edges")
            }
