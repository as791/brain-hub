from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from typer.testing import CliRunner

from brainhub.api import ApiSettings, create_app
from brainhub.cli import app as cli_app
from brainhub.cli import default_db_path
from brainhub.crypto import ContentCipher, MemoryKeyProvider
from brainhub.demo import seed_demo
from brainhub.graph import GraphNotFoundError
from brainhub.models import BrainEvent
from brainhub.policy import CapturePolicyError
from brainhub.projector import SUPPORTED_EVENT_TYPES
from brainhub.redaction import contains_absolute_path, redact_text
from brainhub.service import BrainHubService
from brainhub.store import DemoResetRefused, EventStore

from brainhub_adapters.normalize import normalize_capture


def adapter_event(**overrides):
    payload = {
        "hook_event_name": "agent-turn-complete",
        "session_id": "adapter-session",
        "workspace_id": "/Users/example/private/project",
        "brainhub_summary": "Implemented anchored hybrid search",
        "event_id": "hook-001",
        "timestamp": "2026-07-17T10:00:00Z",
        **overrides,
    }
    return normalize_capture("codex", payload, mode="hook").as_dict()


def test_adapter_event_projects_and_is_searchable_through_http(service):
    event = adapter_event()
    assert event["type"] in SUPPORTED_EVENT_TYPES
    client = TestClient(create_app(service))
    response = client.post(
        "/v1/events", json=event, headers={"Idempotency-Key": event["id"]}
    )
    assert response.status_code == 201
    assert response.json()["accepted"] is True
    replay = client.post(
        "/v1/events", json=event, headers={"Idempotency-Key": event["id"]}
    )
    assert replay.status_code == 200
    assert replay.json()["accepted"] is False
    graph = client.get("/v1/graph").json()
    assert len(graph["nodes"]) >= 4
    assert len(graph["edges"]) >= 2
    search = client.post(
        "/v1/search", json={"query": "anchored hybrid", "scope": "global"}
    )
    assert search.status_code == 200
    assert search.json()["search_mode"] == "lexical_degraded"
    assert search.json()["results"]


def test_write_does_not_rebuild_search_until_the_next_search(service, monkeypatch):
    rebuilds = 0
    original = service.search_index.rebuild

    def counted_rebuild():
        nonlocal rebuilds
        rebuilds += 1
        return original()

    monkeypatch.setattr(service.search_index, "rebuild", counted_rebuild)
    service.record(BrainEvent.model_validate(adapter_event(event_id="lazy-search")))
    assert rebuilds == 0
    assert service.search_index.projection_version == -1
    results = service.search("anchored hybrid", global_scope=True)
    assert results.results
    assert rebuilds == 1
    assert service.search_index.projection_version == service.store.projection_version()


def test_idempotency_header_and_same_id_conflict(service):
    event = adapter_event()
    client = TestClient(create_app(service))
    assert client.post(
        "/v1/events", json=event, headers={"Idempotency-Key": "wrong-key"}
    ).status_code == 400
    assert client.post("/v1/events", json=event).status_code == 201
    event["data"]["summary"] = "different summary"
    assert client.post("/v1/events", json=event).status_code == 409


def test_strict_anchored_search_never_falls_back_global(service):
    seed_demo(service)
    client = TestClient(create_app(service))
    missing_anchor = client.post(
        "/v1/search", json={"query": "privacy", "scope": "anchored"}
    )
    assert missing_anchor.status_code == 422
    omitted_scope = client.post("/v1/search", json={"query": "privacy"})
    assert omitted_scope.status_code == 422
    unknown_anchor = client.post(
        "/v1/search",
        json={"query": "privacy", "scope": "anchored", "anchor_id": "not-a-node"},
    )
    assert unknown_anchor.status_code == 404
    scoped = client.post(
        "/v1/search",
        json={"query": "privacy", "scope": "anchored", "anchor_id": "ws-brain", "hops": 1},
    )
    assert scoped.status_code == 200
    assert scoped.json()["scope"] == "anchored"
    allowed = {node["id"] for node in client.get("/v1/nodes/ws-brain/expand?hops=1").json()["nodes"]}
    assert {item["node"]["id"] for item in scoped.json()["results"]} <= allowed


def test_degraded_anchored_search_requires_a_text_match_before_graph_boost(service):
    seed_demo(service)

    nonsense = service.search(
        "zzzxqv-no-such-concept",
        anchor_id="ws-brain",
        hops=2,
    )
    matched = service.search(
        "graph",
        anchor_id="ws-brain",
        hops=2,
    )

    assert nonsense.search_mode == "lexical_degraded"
    assert nonsense.results == []
    assert matched.results
    assert any(result.graph_score > 0 for result in matched.results)


def test_temporal_and_node_kind_search_filters_and_forbidden_extras(service):
    seed_demo(service)
    client = TestClient(create_app(service))
    response = client.post(
        "/v1/search",
        json={
            "query": "graph search",
            "anchor_id": "ws-brain",
            "scope": "anchored",
            "valid_at": "2026-07-17T09:40:00Z",
            "filters": {"kinds": ["Topic"]},
        },
    )
    assert response.status_code == 200
    assert response.json()["results"]
    assert {hit["node"]["type"] for hit in response.json()["results"]} == {"TOPIC"}
    before_graph = client.post(
        "/v1/search",
        json={
            "query": "graph",
            "scope": "global",
            "valid_at": "2020-01-01T00:00:00Z",
        },
    )
    assert before_graph.status_code == 200
    assert before_graph.json()["results"] == []
    unsupported = client.post(
        "/v1/search",
        json={
            "query": "graph",
            "scope": "global",
            "filters": {"confidence_classes": ["EXTRACTED"]},
        },
    )
    assert unsupported.status_code == 422
    assert client.post(
        "/v1/path",
        json={"source_id": "ws-brain", "target_id": "topic-graph", "surprise": True},
    ).status_code == 422


def test_capture_policy_rejects_transcripts_and_credentials(service):
    raw = adapter_event()
    raw["data"]["transcript"] = "private conversation"
    response = TestClient(create_app(service)).post("/v1/events", json=raw)
    assert response.status_code == 422
    assert "raw content field" in response.json()["detail"]
    assert service.store.counts()["events"] == 0

    secret = adapter_event(event_id="hook-secret")
    secret["id"] = "evt_secret_case_000000000000000000000000000000"
    secret["data"]["summary"] = "Authorization: Bearer abcdefghijklmnopqrstuvwxyz"
    response = TestClient(create_app(service)).post("/v1/events", json=secret)
    assert response.status_code == 422
    assert "credential-like" in response.json()["detail"]
    assert service.store.counts()["events"] == 0


@pytest.mark.parametrize(
    "secret",
    [
        "sk-proj-" + "ABCDEFGHIJKLMNOPQRSTUVWXYZ123456",
        "AKIA" + "ABCDEFGHIJKLMNOP",
        "ghp_" + "ABCDEFGHIJKLMNOPQRSTUVWXYZ123456",
        "xoxb-" + "1234567890-abcdefghijklmnop",
        "eyJ" + "abcdefghijk.abcdefghijk.abcdefgh",
        "AIza" + "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
        "-----BEGIN " + "PRIVATE KEY-----\nabc123\n-----END PRIVATE KEY-----",
    ],
)
def test_standalone_credentials_are_rejected_before_api_storage(service, secret):
    raw = adapter_event(event_id=f"secret-{len(secret)}-{secret[:4]}")
    raw["data"]["summary"] = f"Captured result: {secret}"

    response = TestClient(create_app(service)).post("/v1/events", json=raw)

    assert response.status_code == 422
    assert "credential-like" in response.json()["detail"]
    assert service.store.counts() == {"events": 0, "nodes": 0, "edges": 0}
    assert service.store.connection.execute("SELECT COUNT(*) FROM sync_spool").fetchone()[0] == 0


def test_standalone_credential_is_rejected_by_direct_service_before_storage(service):
    secret = "sk-proj-" + "directserviceABCDEFGHIJKLMNOPQRSTUVWXYZ"
    raw = adapter_event(event_id="direct-secret")
    raw["data"]["summary"] = secret
    event = BrainEvent.model_validate(raw)

    with pytest.raises(CapturePolicyError, match="credential-like"):
        service.record(event)

    assert service.store.get_event(event.id) is None
    assert service.store.counts() == {"events": 0, "nodes": 0, "edges": 0}
    assert service.store.connection.execute("SELECT COUNT(*) FROM sync_spool").fetchone()[0] == 0
    assert secret not in json.dumps(service.search_index._documents)


def test_legacy_policy_bypass_redacts_search_and_sync_defense_in_depth(service):
    secret = "sk-proj-" + "legacybypassABCDEFGHIJKLMNOPQRSTUVWXYZ"
    raw = adapter_event(event_id="legacy-secret")
    raw["data"]["summary"] = secret
    event = BrainEvent.model_validate(raw)

    # Simulate a pre-policy legacy writer that called the encrypted store directly.
    service.store.append_event(event, service.projector)
    service.search_index.rebuild()
    batch = service.next_sync_batch()

    assert batch is not None
    assert secret not in json.dumps(service.search_index._documents)
    assert secret not in json.dumps(batch.model_dump(mode="json"))
    assert "[REDACTED]" in json.dumps(batch.model_dump(mode="json"))


@pytest.mark.parametrize(
    "path",
    [
        "/srv/acme/private/plan.md",
        "/Volumes/Workspace/private/plan.md",
        r"Z:\work\acme\private\plan.md",
        r"\\fileserver\private\plan.md",
        "file:///custom/root/private/plan.md",
    ],
)
def test_shared_redaction_masks_broad_absolute_paths(path):
    text = f"Observed artifact at {path}"

    assert contains_absolute_path(text)
    assert path not in redact_text(text)
    assert "opaque://local-path" in redact_text(text)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", "/srv/acme/private/event.json"),
        ("source", "file:///srv/acme/private/source.json"),
        ("subject", r"C:\work\acme\private\subject.txt"),
        ("subject", "sk-proj-" + "envelopeABCDEFGHIJKLMNOPQRSTUVWXYZ"),
    ],
)
def test_plaintext_envelope_identifiers_reject_paths_and_secrets(
    service, field, value
):
    raw = adapter_event(event_id=f"unsafe-envelope-{field}")
    raw[field] = value

    response = TestClient(create_app(service)).post("/v1/events", json=raw)

    assert response.status_code == 422
    assert service.store.counts() == {"events": 0, "nodes": 0, "edges": 0}


def test_explicit_graph_identifier_rejects_standalone_secret_before_append(service):
    raw = adapter_event(event_id="unsafe-explicit-id")
    raw["type"] = "com.brainhub.graph.imported.v1"
    raw["data"]["nodes"] = [
        {
            "id": "ghp_" + "ABCDEFGHIJKLMNOPQRSTUVWXYZ123456",
            "type": "TOPIC",
            "title": "Unsafe identifier",
        }
    ]

    response = TestClient(create_app(service)).post("/v1/events", json=raw)

    assert response.status_code == 422
    assert "credential-like" in response.json()["detail"]
    assert service.store.counts() == {"events": 0, "nodes": 0, "edges": 0}

    raw = adapter_event(event_id="unsafe-semantic-id")
    raw["data"]["topics"] = [
        {"id": "/srv/acme/private/topic.md", "title": "Unsafe topic identifier"}
    ]
    response = TestClient(create_app(service)).post("/v1/events", json=raw)
    assert response.status_code == 422
    assert "data.topics.0.id" in response.json()["detail"]
    assert service.store.counts() == {"events": 0, "nodes": 0, "edges": 0}


def test_capture_policy_applies_to_top_level_cloudevent_extensions(service):
    client = TestClient(create_app(service))
    transcript = adapter_event(event_id="extension-transcript")
    transcript["transcript"] = "private top-level transcript"
    response = client.post("/v1/events", json=transcript)
    assert response.status_code == 422
    assert "extensions.transcript" in response.json()["detail"]

    forbidden = adapter_event(event_id="extension-secret")
    forbidden["secret"] = "not-for-storage"
    response = client.post("/v1/events", json=forbidden)
    assert response.status_code == 422
    assert "extensions.secret" in response.json()["detail"]

    secret_value = adapter_event(event_id="extension-secret-value")
    secret_value["correlationid"] = "Authorization: Bearer abcdefghijklmnopqrstuvwxyz"
    response = client.post("/v1/events", json=secret_value)
    assert response.status_code == 422
    assert "credential-like" in response.json()["detail"]
    assert service.store.counts()["events"] == 0

    safe = adapter_event(event_id="extension-safe")
    safe["traceparent"] = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    safe["partitionkey"] = "workspace-opaque"
    response = client.post("/v1/events", json=safe)
    assert response.status_code == 201
    stored = service.store.get_event(safe["id"])
    assert stored is not None
    assert stored.model_extra["traceparent"] == safe["traceparent"]


def test_feedback_request_forbids_unknown_fields(service):
    response = TestClient(create_app(service)).post(
        "/v1/feedback",
        json={
            "target_id": "node-target",
            "verdict": "accept",
            "unexpected": "silently dropping this would be unsafe",
        },
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    "graph_facts",
    [
        {"nodes": [{"id": "bad-node", "type": "TOPIC"}]},
        {
            "nodes": [
                {"id": "node-one", "type": "TOPIC", "title": "One"},
                {"id": "node-two", "type": "TOPIC", "title": "Two"},
            ],
            "edges": [
                {
                    "id": "bad-edge",
                    "source_id": "node-one",
                    "target_id": "node-two",
                    "relation": "REFERENCES",
                    "explanation": "One references two.",
                }
            ],
        },
    ],
)
def test_invalid_explicit_graph_facts_return_422_and_roll_back(service, graph_facts):
    raw = adapter_event(event_id="invalid-explicit-graph")
    raw["type"] = "com.brainhub.graph.imported.v1"
    raw["data"].update(graph_facts)

    response = TestClient(create_app(service)).post("/v1/events", json=raw)

    assert response.status_code == 422
    assert "invalid graph facts" in response.json()["detail"]
    assert service.store.counts() == {"events": 0, "nodes": 0, "edges": 0}
    assert service.store.connection.execute("SELECT COUNT(*) FROM sync_spool").fetchone()[0] == 0


def test_missing_feedback_target_is_404_and_persists_no_event(service):
    request = {"target_id": "missing-target", "verdict": "accept"}

    with pytest.raises(GraphNotFoundError, match="was not found"):
        service.feedback(request)
    response = TestClient(create_app(service)).post("/v1/feedback", json=request)

    assert response.status_code == 404
    assert service.store.counts() == {"events": 0, "nodes": 0, "edges": 0}
    assert service.store.connection.execute("SELECT COUNT(*) FROM sync_spool").fetchone()[0] == 0


def test_auth_cors_body_limit_and_websocket_first_frame(service):
    client = TestClient(
        create_app(service, settings=ApiSettings(token="test-token", max_content_length=1000))
    )
    assert client.get("/healthz").status_code == 200
    assert client.get("/v1/graph").status_code == 401
    preflight = client.options(
        "/v1/events",
        headers={
            "Origin": "http://127.0.0.1:4173",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "http://127.0.0.1:4173"
    assert client.post(
        "/v1/events",
        content=b"x" * 1001,
        headers={"Authorization": "Bearer test-token", "Content-Type": "application/json"},
    ).status_code == 413
    with client.websocket_connect("/ws") as websocket:
        websocket.send_json({"type": "brainhub.auth", "token": "test-token"})
        assert websocket.receive_json()["type"] == "projection.ready"
    with client.websocket_connect(
        "/ws", headers={"Origin": "https://attacker.example"}
    ) as websocket:
        with pytest.raises(WebSocketDisconnect) as closed:
            websocket.receive_json()
        assert closed.value.code == 4403


def test_demo_graph_cache_invalidates_on_write(service):
    seed_demo(service)
    first = service.get_graph()
    cached_version = service.graph._cache_version
    assert first.anchor_id is None
    service.record(BrainEvent.model_validate(adapter_event(event_id="hook-002")))
    second = service.get_graph()
    assert service.graph._cache_version > cached_version
    assert len(second.nodes) > len(first.nodes)


def test_two_services_refresh_search_and_graph_after_external_writes(tmp_path):
    database = tmp_path / "shared.db"
    key = bytes(range(32))
    first = BrainHubService(
        EventStore(database, ContentCipher(MemoryKeyProvider(key))), enable_semantic=False
    )
    second = BrainHubService(
        EventStore(database, ContentCipher(MemoryKeyProvider(key))), enable_semantic=False
    )
    try:
        first.record(BrainEvent.model_validate(adapter_event(event_id="external-alpha")))
        alpha = second.search("anchored hybrid", global_scope=True)
        assert alpha.results
        first_graph = second.get_graph()
        cached_version = second.graph.cache_version

        beta_event = adapter_event(
            event_id="external-beta",
            session_id="external-beta-session",
            brainhub_summary="Fresh external beta marker",
        )
        first.record(BrainEvent.model_validate(beta_event))
        beta = second.search("external beta marker", global_scope=True)
        assert beta.results
        second_graph = second.get_graph()
        assert second.graph.cache_version > cached_version
        assert len(second_graph.nodes) > len(first_graph.nodes)
    finally:
        first.close()
        second.close()


def test_websocket_polls_projection_version_for_external_process_writes(tmp_path):
    database = tmp_path / "shared-websocket.db"
    key = bytes(range(32))
    api_service = BrainHubService(
        EventStore(database, ContentCipher(MemoryKeyProvider(key))), enable_semantic=False
    )
    writer = BrainHubService(
        EventStore(database, ContentCipher(MemoryKeyProvider(key))), enable_semantic=False
    )
    settings = ApiSettings(token="test-token", websocket_poll_interval_seconds=0.05)
    try:
        with TestClient(create_app(api_service, settings=settings)).websocket_connect(
            "/ws"
        ) as websocket:
            websocket.send_json({"type": "brainhub.auth", "token": "test-token"})
            ready = websocket.receive_json()
            writer.record(BrainEvent.model_validate(adapter_event(event_id="ws-external")))
            update = websocket.receive_json()
            assert update["type"] == "projection.updated"
            assert update["projection_version"] > ready["projection_version"]
            assert update["external_process"] is True
    finally:
        api_service.close()
        writer.close()


def test_recorded_time_uses_ingestion_not_unknown_occurrence_sentinel(service):
    before = datetime.now(UTC)
    event = normalize_capture(
        "codex",
        {
            "hook_event_name": "unknown",
            "session_id": "unknown-time-session",
            "workspace_id": "opaque-workspace",
            "event_id": "unknown-time-event",
        },
        mode="hook",
    ).as_dict()
    assert event["time"] == "1970-01-01T00:00:00Z"
    service.record(BrainEvent.model_validate(event))
    after = datetime.now(UTC)
    run = next(
        node
        for node in service.store.list_nodes()
        if node.type.value == "RUN" and "unknown-time-session" in node.external_ids
    )
    assert run.valid_time.start == datetime(1970, 1, 1, tzinfo=UTC)
    assert before <= run.recorded_time.start <= after


def test_canonical_database_environment_variable(monkeypatch, tmp_path):
    legacy = tmp_path / "legacy.db"
    canonical = tmp_path / "canonical.db"
    monkeypatch.setenv("BRAINHUB_DB", str(legacy))
    monkeypatch.setenv("BRAINHUB_DB_PATH", str(canonical))
    assert default_db_path() == canonical


def test_cli_refuses_unauthenticated_non_loopback_bind():
    result = CliRunner().invoke(
        cli_app,
        ["serve", "--host", "0.0.0.0", "--allow-non-loopback"],
        env={"BRAINHUB_API_TOKEN": ""},
    )

    assert result.exit_code != 0
    assert "nonempty BRAINHUB_API_TOKEN" in result.output


def test_demo_reset_refuses_personal_events(service):
    service.record(BrainEvent.model_validate(adapter_event(event_id="personal-event")))
    from brainhub.demo import demo_event

    try:
        service.store.reset_if_only_events({demo_event().id})
    except DemoResetRefused:
        pass
    else:  # pragma: no cover - safety invariant
        raise AssertionError("personal database reset was not refused")
    assert service.store.counts()["events"] == 1
