from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jsonschema import validate

from brainhub.api import create_app
from brainhub.crypto import ContentCipher, MemoryKeyProvider
from brainhub.graphify import GraphifyImporter
from brainhub.mcp_server import create_mcp_server
from brainhub.models import BrainEvent, ConfidenceClass, sha256_hex
from brainhub.projector import Projector
from brainhub.service import BrainHubService
from brainhub.store import EventStore, ProjectionIntegrityError


ROOT = Path(__file__).resolve().parents[3]
FIXTURE = Path(__file__).parent / "fixtures" / "graphify-out" / "graph.json"


def test_graphify_json_preserves_audit_confidence_and_reifies_hyperedge(service):
    result = service.import_graphify(FIXTURE)
    assert result.accepted
    graph = service.get_graph()
    assert len(graph.nodes) == 4
    assert len(graph.edges) == 6
    assert {edge.confidence_class for edge in graph.edges} == {
        ConfidenceClass.EXTRACTED,
        ConfidenceClass.INFERRED,
        ConfidenceClass.AMBIGUOUS,
    }
    hyperedge = next(node for node in graph.nodes if node.properties.get("reified_hyperedge"))
    participants = [
        edge for edge in graph.edges if edge.target_id == hyperedge.id and edge.relation.value == "PARTICIPATES_IN"
    ]
    assert len(participants) == 3
    serialized = json.dumps(graph.model_dump(mode="json"))
    assert "/Users/private" not in serialized
    assert "brainhub://artifact/" in serialized
    edge = next(
        edge
        for edge in graph.edges
        if edge.properties.get("graphify_original_id") == "edge-inferred"
    )
    assert edge.extractor == "graphify"
    assert edge.extractor_version == "0.9.16"
    assert edge.properties["graphify_source_hash"].startswith("aaaa")
    assert all(
        node.id.startswith(("graphify-node:", "graphify-hyperedge:"))
        for node in graph.nodes
    )
    assert all(
        edge.id.startswith(("graphify-edge:", "graphify-participant-edge:"))
        for edge in graph.edges
    )
    assert not {
        "node-a",
        "node-b",
        "node-c",
        "edge-extracted",
        "edge-inferred",
        "edge-ambiguous",
    } & {fact.id for fact in [*graph.nodes, *graph.edges]}


def test_graphify_missing_confidence_is_ambiguous_and_cli_command_is_public(tmp_path):
    graph = {
        "version": "0.9.16",
        "nodes": [{"id": "one", "label": "One"}, {"id": "two", "label": "Two"}],
        "edges": [{"source": "one", "target": "two", "relation": "REFERENCES"}],
    }
    path = tmp_path / "graph.json"
    path.write_text(json.dumps(graph), encoding="utf-8")
    event = GraphifyImporter().to_event(path)
    edge = event.data.model_extra["edges"][0]
    assert edge["confidence_class"] == "AMBIGUOUS"
    assert edge["confidence_score"] <= 0.3
    assert GraphifyImporter.build_cli_command("paper.pdf", "out") == [
        "graphify",
        "extract",
        "paper.pdf",
        "--out",
        "out",
    ]


def test_graphify_canonical_ids_are_hashed_and_stable():
    imported_at = "2026-07-17T10:00:00+00:00"
    first = GraphifyImporter().to_event(
        FIXTURE, imported_at=datetime.fromisoformat(imported_at)
    )
    second = GraphifyImporter().to_event(
        FIXTURE, imported_at=datetime.fromisoformat(imported_at)
    )
    first_extras = first.data.model_extra
    second_extras = second.data.model_extra

    assert [node["id"] for node in first_extras["nodes"]] == [
        node["id"] for node in second_extras["nodes"]
    ]
    assert [edge["id"] for edge in first_extras["edges"]] == [
        edge["id"] for edge in second_extras["edges"]
    ]
    assert all(
        node["id"] != node["properties"]["graphify_original_id"]
        for node in first_extras["nodes"]
    )
    assert all(
        edge["id"] != edge["properties"].get("graphify_original_id")
        for edge in first_extras["edges"]
    )


def test_graph_only_sync_batch_matches_schema_and_excludes_raw_content(service):
    service.import_graphify(FIXTURE)
    stored = service.store.connection.execute(
        "SELECT event_id, canonical_sha256, graph_payload FROM sync_spool"
    ).fetchone()
    assert isinstance(stored["graph_payload"], bytes)
    assert stored["graph_payload"].startswith(ContentCipher.VERSION)
    assert b"Capture" not in stored["graph_payload"]
    batch = service.next_sync_batch()
    assert batch is not None
    payload = batch.model_dump(mode="json")
    schema = json.loads((ROOT / "schemas" / "sync-batch.schema.json").read_text())
    validate(payload, schema)
    serialized = json.dumps(payload)
    assert "/Users/private" not in serialized
    assert '"content"' not in serialized
    graph_payload = payload["events"][0]["graph_payload"]
    assert payload["events"][0]["canonical_sha256"] == sha256_hex(graph_payload)
    raw_event_hash = service.store.connection.execute(
        "SELECT canonical_sha256 FROM events"
    ).fetchone()["canonical_sha256"]
    assert payload["events"][0]["canonical_sha256"] != raw_event_hash
    assert graph_payload["nodes"][0]["title"]
    assert graph_payload["edges"][0]["explanation"]
    assert graph_payload["edges"][0]["extractor"] == "graphify"
    assert all(value.startswith("external:") for node in graph_payload["nodes"] for value in node["external_ids"])
    assert service.acknowledge_sync(batch.last_sequence) == 1
    assert service.next_sync_batch() is None


def test_unsupported_versioned_event_emits_schema_valid_empty_sync_payload(service):
    event = BrainEvent.create(
        source="urn:brainhub-test:future",
        type="com.brainhub.future.observed.v9",
        subject="future/empty-projection",
        time=datetime.fromisoformat("2026-07-17T10:00:00+00:00"),
        data={
            "agent": {"product": "future-agent", "surface": "test"},
            "workspace_id": "workspace-safe",
            "session_id": "session-safe",
            "status": "completed",
            "summary": "A future event retained without graph facts.",
            "capture": {"mode": "hook", "content_level": "summary"},
        },
    )
    service.record(event)

    batch = service.next_sync_batch()
    assert batch is not None
    payload = batch.model_dump(mode="json")
    validate(
        payload,
        json.loads((ROOT / "schemas" / "sync-batch.schema.json").read_text()),
    )
    assert payload["events"][0]["graph_payload"] == {
        "projector": "1.0.0",
        "source_sequence": 1,
        "nodes": [],
        "edges": [],
    }


def test_sync_hashes_evidence_paths_and_pseudonymizes_safe_local_ids(service):
    private_path = "/srv/acme/private/secret.py"
    artifact_id = "artifact-local-safe"
    topic_id = "topic-local-safe"
    evidence = {
        "locator": private_path,
        "anchor": f"{private_path}#L12",
        "content_hash": "a" * 64,
        "visibility": "SYNCABLE",
    }
    event = {
        "specversion": "1.0",
        "id": "event-path-privacy-0001",
        "source": "urn:brainhub-test:path-privacy",
        "type": "com.brainhub.graph.imported.v1",
        "subject": "graph/path-privacy",
        "time": "2026-07-17T10:00:00Z",
        "datacontenttype": "application/json",
        "data": {
            "agent": {"product": "codex", "surface": "cli", "version": "1.0"},
            "workspace_id": "workspace-safe",
            "session_id": "session-safe",
            "status": "completed",
            "summary": f"Imported two linked concepts from {private_path}.",
            "capture": {"mode": "import", "content_level": "summary"},
            "nodes": [
                {
                    "id": artifact_id,
                    "type": "ARTIFACT",
                    "title": "Private artifact",
                    "summary": f"An opaque local artifact at {private_path}.",
                    "external_id": "artifact-external-safe",
                    "content_hash": "b" * 64,
                    "actor_id": "actor-safe",
                    "evidence": [evidence],
                },
                {
                    "id": topic_id,
                    "type": "TOPIC",
                    "title": "Related topic",
                    "summary": "A linked semantic topic.",
                    "evidence": [evidence],
                },
            ],
            "edges": [
                {
                    "id": "edge-local-safe",
                    "source_id": artifact_id,
                    "target_id": topic_id,
                    "relation": "REFERENCES",
                    "explanation": "The artifact references this topic.",
                    "confidence_class": "EXTRACTED",
                    "confidence_score": 1.0,
                    "actor_id": "actor-safe",
                    "evidence": [evidence],
                }
            ],
        },
    }

    response = TestClient(create_app(service)).post("/v1/events", json=event)
    assert response.status_code == 201
    assert service.get_node(artifact_id).id == artifact_id
    batch = service.next_sync_batch()
    assert batch is not None
    payload = batch.model_dump(mode="json")
    validate(
        payload,
        json.loads((ROOT / "schemas" / "sync-batch.schema.json").read_text()),
    )

    serialized = json.dumps(payload)
    assert private_path not in serialized
    assert "/srv/acme" not in serialized
    sync_event = payload["events"][0]
    graph_payload = sync_event["graph_payload"]
    assert sync_event["event_id"].startswith("cloud-event:")
    assert {
        node["source_event_id"] for node in graph_payload["nodes"]
    } == {sync_event["event_id"]}
    assert {
        edge["source_event_id"] for edge in graph_payload["edges"]
    } == {sync_event["event_id"]}
    assert {
        reference["source_event_id"]
        for fact in [*graph_payload["nodes"], *graph_payload["edges"]]
        for reference in fact["evidence"]
    } == {sync_event["event_id"]}
    cloud_node_ids = {node["id"] for node in graph_payload["nodes"]}
    assert all(node_id.startswith("cloud-node:") for node_id in cloud_node_ids)
    edge = graph_payload["edges"][0]
    assert {edge["source_id"], edge["target_id"]} <= cloud_node_ids
    assert edge["id"].startswith("cloud-edge:")
    assert all(
        reference["opaque_uri"].startswith("brainhub://evidence/")
        and reference["anchor"].startswith("brainhub://anchor/")
        for fact in [*graph_payload["nodes"], *graph_payload["edges"]]
        for reference in fact["evidence"]
    )


def test_api_rejects_absolute_paths_in_event_and_explicit_graph_identifiers(service):
    event = {
        "specversion": "1.0",
        "id": "/srv/acme/private/event.json",
        "source": "urn:brainhub-test:path-policy",
        "type": "com.brainhub.graph.imported.v1",
        "subject": "graph/path-policy",
        "time": "2026-07-17T10:00:00Z",
        "datacontenttype": "application/json",
        "data": {
            "agent": {"product": "codex", "surface": "cli", "version": "1.0"},
            "workspace_id": "workspace-safe",
            "session_id": "session-safe",
            "status": "completed",
            "summary": "Attempted path-shaped graph identifiers.",
            "capture": {"mode": "import", "content_level": "summary"},
            "nodes": [
                {
                    "id": r"D:\private\artifact.py",
                    "type": "ARTIFACT",
                    "title": "Private artifact",
                }
            ],
        },
    }

    response = TestClient(create_app(service)).post("/v1/events", json=event)

    assert response.status_code == 422
    assert "absolute path identifier" in response.json()["detail"]
    assert service.store.counts() == {"events": 0, "nodes": 0, "edges": 0}

    event["id"] = "event-path-policy-0001"
    response = TestClient(create_app(service)).post("/v1/events", json=event)
    assert response.status_code == 422
    assert "data.nodes.0.id" in response.json()["detail"]
    assert service.store.counts() == {"events": 0, "nodes": 0, "edges": 0}


def test_legacy_plaintext_sync_payload_is_encrypted_during_open(tmp_path):
    database = tmp_path / "legacy-sync.db"
    key = bytes(range(32))
    first = BrainHubService(
        EventStore(database, ContentCipher(MemoryKeyProvider(key))), enable_semantic=False
    )
    first.import_graphify(FIXTURE)
    batch = first.next_sync_batch()
    assert batch is not None
    graph_payload = batch.events[0].graph_payload
    first.store.connection.execute(
        "UPDATE sync_spool SET graph_payload = ?, canonical_sha256 = ?",
        (json.dumps(graph_payload), "0" * 64),
    )
    first.close()

    reopened = EventStore(database, ContentCipher(MemoryKeyProvider(key)))
    row = reopened.connection.execute(
        "SELECT graph_payload, canonical_sha256 FROM sync_spool"
    ).fetchone()
    assert isinstance(row["graph_payload"], bytes)
    assert row["graph_payload"].startswith(ContentCipher.VERSION)
    assert row["canonical_sha256"] == sha256_hex(graph_payload)
    assert reopened.next_sync_batch().events[0].graph_payload == graph_payload
    reopened.close()


def test_sync_migration_retries_busy_checkpoint_without_repseudonymizing(
    monkeypatch, tmp_path
):
    database = tmp_path / "busy-sync-checkpoint.db"
    key = bytes(range(32))
    first = EventStore(database, ContentCipher(MemoryKeyProvider(key)))
    first.append_event(
        BrainEvent.create(
            source="urn:brainhub-test:sync-migration",
            type="com.brainhub.future.observed.v9",
            subject="future/sync-migration",
            time=datetime.fromisoformat("2026-07-17T10:00:00+00:00"),
            data={
                "agent": {"product": "future-agent", "surface": "test"},
                "workspace_id": "workspace-safe",
                "session_id": "session-safe",
                "status": "completed",
                "summary": "Migration checkpoint test.",
                "capture": {"mode": "hook", "content_level": "summary"},
            },
        ),
        Projector(first),
    )
    original_event_id = first.connection.execute(
        "SELECT event_id FROM sync_spool"
    ).fetchone()["event_id"]
    first.connection.execute(
        "UPDATE metadata SET value = '2' WHERE key = 'sync_boundary_version'"
    )
    truncate_wal = EventStore._truncate_wal
    monkeypatch.setattr(EventStore, "_truncate_wal", lambda _self: (1, 0, 0))

    with pytest.raises(ProjectionIntegrityError, match="sync WAL"):
        first._migrate_sync_payloads()

    migrated_event_id = first.connection.execute(
        "SELECT event_id FROM sync_spool"
    ).fetchone()["event_id"]
    assert migrated_event_id != original_event_id
    assert first.connection.execute(
        "SELECT value FROM metadata WHERE key = ?",
        (EventStore.SYNC_CHECKPOINT_PENDING,),
    ).fetchone()["value"] == "1"
    first.close()
    monkeypatch.setattr(EventStore, "_truncate_wal", truncate_wal)

    reopened = EventStore(database, ContentCipher(MemoryKeyProvider(key)))
    assert reopened.connection.execute(
        "SELECT event_id FROM sync_spool"
    ).fetchone()["event_id"] == migrated_event_id
    assert reopened.connection.execute(
        "SELECT 1 FROM metadata WHERE key = ?",
        (EventStore.SYNC_CHECKPOINT_PENDING,),
    ).fetchone() is None
    assert (
        reopened.next_sync_batch().events[0].event_id
        == migrated_event_id
    )
    reopened.close()


def test_sync_rejects_acknowledgement_that_was_never_issued(service):
    service.import_graphify(FIXTURE)
    with pytest.raises(ValueError, match="issued pending batch"):
        service.acknowledge_sync(999)


@pytest.mark.asyncio
async def test_mcp_exposes_exact_six_typed_non_destructive_tools(service):
    server = create_mcp_server(service)
    tools = await server.list_tools()
    assert server._mcp_server.version == "0.1.0"
    assert {tool.name for tool in tools} == {
        "brainhub.record",
        "brainhub.search",
        "brainhub.get_node",
        "brainhub.expand",
        "brainhub.path",
        "brainhub.feedback",
    }
    for tool in tools:
        assert tool.outputSchema is not None
        assert tool.annotations is not None
        assert tool.annotations.destructiveHint is False
    assert next(tool for tool in tools if tool.name == "brainhub.search").annotations.readOnlyHint
    search_tool = next(tool for tool in tools if tool.name == "brainhub.search")
    assert search_tool.inputSchema["properties"]["scope"]["default"] == "anchored"
