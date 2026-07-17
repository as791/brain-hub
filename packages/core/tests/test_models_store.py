from __future__ import annotations

import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from nacl.exceptions import CryptoError
from pydantic import ValidationError

from brainhub.crypto import ContentCipher, KeyringKeyProvider, MemoryKeyProvider
from brainhub.models import (
    BrainEvent,
    Edge,
    deterministic_event_id,
)
from brainhub.projector import Projector
from brainhub.store import EventIntegrityError, EventStore, ProjectionIntegrityError


NOW = datetime(2026, 7, 17, 10, 0, tzinfo=UTC)


def event_data(**extras):
    return {
        "agent": {"product": "codex", "surface": "cli", "version": "1.0"},
        "workspace_id": "workspace-opaque",
        "session_id": "session-one",
        "status": "completed",
        "summary": "Implemented an idempotent projection.",
        "capture": {"mode": "hook", "content_level": "summary"},
        **extras,
    }


def make_event(**extras) -> BrainEvent:
    return BrainEvent.create(
        source="urn:brainhub-test:codex",
        type="com.brainhub.workstream.turn.completed.v1",
        subject="workstream/brain-hub",
        time=NOW,
        data=event_data(**extras),
    )


def test_deterministic_cloudevent_and_contract_validation():
    first = make_event()
    second = make_event()
    assert first.id == second.id
    assert first.id == deterministic_event_id(
        source=first.source,
        event_type=first.type,
        subject=first.subject,
        time=first.time,
        data=first.data,
    )
    with pytest.raises(ValidationError):
        BrainEvent.model_validate({**first.model_dump(), "type": "unversioned"})


def test_edge_requires_bounded_explanation_and_evidence():
    payload = {
        "id": "edge-one",
        "source_id": "node-a",
        "target_id": "node-b",
        "relation": "ABOUT",
        "explanation": "x" * 321,
        "confidence_class": "EXTRACTED",
        "confidence_score": 1,
        "evidence": [],
        "valid_time": {"start": NOW},
        "recorded_time": {"start": NOW},
        "creation_event_id": "event-0001",
        "latest_revision_id": "event-0001",
    }
    with pytest.raises(ValidationError):
        Edge.model_validate(payload)


def test_xchacha_encryption_authenticates_context_and_ciphertext():
    cipher = ContentCipher(MemoryKeyProvider(bytes(range(32))))
    encrypted = cipher.encrypt(b"private summary", context=b"node:a")
    assert b"private summary" not in encrypted
    assert cipher.decrypt(encrypted, context=b"node:a") == b"private summary"
    with pytest.raises(CryptoError):
        cipher.decrypt(encrypted, context=b"node:b")
    tampered = encrypted[:-1] + bytes([encrypted[-1] ^ 1])
    with pytest.raises(CryptoError):
        cipher.decrypt(tampered, context=b"node:a")


def test_cloud_pseudonyms_are_stable_per_installation_and_unlinkable_across_keys():
    first = ContentCipher(MemoryKeyProvider(bytes(range(32))))
    same_installation = ContentCipher(MemoryKeyProvider(bytes(range(32))))
    different_installation = ContentCipher(MemoryKeyProvider(bytes(reversed(range(32)))))

    pseudonym = first.pseudonymize("node", "/Users/alice/private/project")

    assert pseudonym == first.pseudonymize("node", "/Users/alice/private/project")
    assert pseudonym == same_installation.pseudonymize(
        "node", "/Users/alice/private/project"
    )
    assert pseudonym != different_installation.pseudonymize(
        "node", "/Users/alice/private/project"
    )
    assert pseudonym != first.pseudonymize("event", "/Users/alice/private/project")


def test_keyring_first_key_creation_is_locked_and_reread(monkeypatch, tmp_path):
    initial_reads = threading.Barrier(2)
    guard = threading.Lock()
    state = {"encoded": None, "reads": 0, "writes": 0}

    def get_password(_service, _installation):
        with guard:
            state["reads"] += 1
            read_number = state["reads"]
        if read_number <= 2:
            initial_reads.wait(timeout=2)
            return None
        with guard:
            return state["encoded"]

    def set_password(_service, _installation, encoded):
        time.sleep(0.05)
        with guard:
            state["writes"] += 1
            state["encoded"] = encoded

    monkeypatch.setitem(
        sys.modules,
        "keyring",
        SimpleNamespace(get_password=get_password, set_password=set_password),
    )
    lock_path = tmp_path / "keyring.lock"
    providers = [
        KeyringKeyProvider("shared-installation", lock_path=lock_path)
        for _ in range(2)
    ]

    with ThreadPoolExecutor(max_workers=2) as executor:
        keys = list(executor.map(lambda provider: provider.get_key(), providers))

    assert keys[0] == keys[1]
    assert len(keys[0]) == 32
    assert state["writes"] == 1
    assert state["reads"] == 4


def test_append_is_idempotent_and_conflicting_reuse_is_rejected(tmp_path):
    cipher = ContentCipher(MemoryKeyProvider(bytes(range(32))))
    store = EventStore(tmp_path / "store.db", cipher)
    projector = Projector(store)
    event = make_event(topics=["idempotency"])
    sequence, accepted, version = store.append_event(event, projector)
    replay_sequence, replay_accepted, replay_version = store.append_event(event, projector)
    assert (sequence, accepted, version) == (1, True, 1)
    assert (replay_sequence, replay_accepted, replay_version) == (1, False, 1)
    assert store.get_event(event.id) == event
    row = store.connection.execute(
        "SELECT encrypted_payload FROM events WHERE event_id = ?", (event.id,)
    ).fetchone()
    assert b"idempotency" not in row["encrypted_payload"]
    external_index_rows = store.connection.execute(
        "SELECT external_ids FROM nodes"
    ).fetchall()
    external_indexes = [
        value
        for index_row in external_index_rows
        for value in json.loads(index_row["external_ids"])
    ]
    assert external_indexes
    assert all(value.startswith("external-index:") for value in external_indexes)
    assert not {"workspace-opaque", "session-one", "workstream/brain-hub"} & set(
        external_indexes
    )

    changed = event.model_copy(
        update={"data": event.data.model_copy(update={"summary": "altered content"})}
    )
    with pytest.raises(EventIntegrityError):
        store.append_event(changed, projector)
    assert store.counts()["events"] == 1
    store.close()


def test_open_migrates_legacy_plaintext_external_id_indexes(tmp_path):
    database = tmp_path / "legacy-node-index.db"
    key = bytes(range(32))
    first = EventStore(database, ContentCipher(MemoryKeyProvider(key)))
    first.append_event(make_event(), Projector(first))
    first.connection.execute(
        "UPDATE nodes SET external_ids = ?",
        ('["/srv/legacy/private/workspace"]',),
    )
    first.connection.execute(
        "UPDATE metadata SET value = '0' WHERE key = 'node_index_boundary_version'"
    )
    first.close()

    reopened = EventStore(database, ContentCipher(MemoryKeyProvider(key)))
    stored = [
        value
        for row in reopened.connection.execute("SELECT external_ids FROM nodes")
        for value in json.loads(row["external_ids"])
    ]

    assert "/srv/legacy/private/workspace" not in stored
    assert all(value.startswith("external-index:") for value in stored)
    reopened.close()


def test_node_index_migration_retries_pending_busy_checkpoint_on_reopen(
    monkeypatch, tmp_path
):
    database = tmp_path / "busy-node-index.db"
    key = bytes(range(32))
    first = EventStore(database, ContentCipher(MemoryKeyProvider(key)))
    first.append_event(make_event(), Projector(first))
    first.connection.execute(
        "UPDATE nodes SET external_ids = ?",
        ('["/srv/legacy/private/workspace"]',),
    )
    first.connection.execute(
        "UPDATE metadata SET value = '0' WHERE key = 'node_index_boundary_version'"
    )
    truncate_wal = EventStore._truncate_wal
    monkeypatch.setattr(EventStore, "_truncate_wal", lambda _self: (1, 0, 0))

    with pytest.raises(ProjectionIntegrityError, match="node-index WAL"):
        first._migrate_node_indexes()

    assert first.connection.execute(
        "SELECT value FROM metadata WHERE key = ?",
        (EventStore.NODE_INDEX_CHECKPOINT_PENDING,),
    ).fetchone()["value"] == "1"
    first.close()
    monkeypatch.setattr(EventStore, "_truncate_wal", truncate_wal)

    reopened = EventStore(database, ContentCipher(MemoryKeyProvider(key)))
    assert reopened.connection.execute(
        "SELECT 1 FROM metadata WHERE key = ?",
        (EventStore.NODE_INDEX_CHECKPOINT_PENDING,),
    ).fetchone() is None
    stored = [
        value
        for row in reopened.connection.execute("SELECT external_ids FROM nodes")
        for value in json.loads(row["external_ids"])
    ]
    assert "/srv/legacy/private/workspace" not in stored
    assert all(value.startswith("external-index:") for value in stored)
    reopened.close()


def test_unknown_event_is_retained_but_does_not_project(service):
    event = BrainEvent.create(
        source="urn:brainhub-test:future",
        type="com.brainhub.future.observed.v9",
        subject="future/1",
        time=NOW,
        data=event_data(),
    )
    result = service.record(event)
    assert result.accepted
    assert service.store.counts() == {"events": 1, "nodes": 0, "edges": 0}


def test_same_titled_semantics_are_not_silently_merged_across_events(service):
    first = BrainEvent.create(
        source="urn:brainhub-test:identity",
        type="com.brainhub.workstream.turn.completed.v1",
        subject="workstream/shared-context",
        time=NOW,
        data=event_data(session_id="session-alpha", topics=["Shared title"]),
    )
    second = BrainEvent.create(
        source="urn:brainhub-test:identity",
        type="com.brainhub.workstream.turn.completed.v1",
        subject="workstream/shared-context",
        time=NOW,
        data=event_data(session_id="session-beta", topics=["Shared title"]),
    )
    service.record(first)
    service.record(second)
    topics = [
        node
        for node in service.store.list_nodes()
        if node.type.value == "TOPIC" and node.title == "Shared title"
    ]
    assert len(topics) == 2
    assert topics[0].id != topics[1].id


def test_same_titled_semantics_in_one_event_keep_distinct_event_ordinals(service):
    event = BrainEvent.create(
        source="urn:brainhub-test:identity",
        type="com.brainhub.workstream.turn.completed.v1",
        subject="workstream/duplicate-titles",
        time=NOW,
        data=event_data(
            session_id="session-duplicate-titles",
            topics=["Shared title", "Shared title"],
        ),
    )

    service.record(event)
    topics = [
        node
        for node in service.store.list_nodes()
        if node.type.value == "TOPIC" and node.title == "Shared title"
    ]

    assert len(topics) == 2
    assert len({node.id for node in topics}) == 2
