from __future__ import annotations

import base64
import json
import os
import stat
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from nacl.exceptions import CryptoError
from pydantic import ValidationError

from brainhub.crypto import (
    ContentCipher,
    DefaultKeyProvider,
    KeyringKeyProvider,
    KeyringUnavailableError,
    KeyUnavailableError,
    MemoryKeyProvider,
)
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


def test_selected_keyring_never_recreates_missing_key(monkeypatch, tmp_path):
    writes = []
    monkeypatch.setitem(
        sys.modules,
        "keyring",
        SimpleNamespace(
            get_password=lambda _service, _installation: None,
            set_password=lambda *_args: writes.append(True),
        ),
    )
    provider = KeyringKeyProvider(
        "missing-keyring-installation",
        lock_path=tmp_path / "keyring.lock",
    )

    with pytest.raises(KeyUnavailableError, match="missing"):
        provider.get_existing_key()
    assert writes == []


def test_default_key_provider_keeps_explicit_environment_override_first(
    monkeypatch,
    tmp_path,
):
    expected = bytes(reversed(range(32)))
    monkeypatch.setattr(sys, "stdin", None)
    monkeypatch.setenv("BRAINHUB_HEADLESS", "not-a-boolean")
    monkeypatch.setenv(
        "BRAINHUB_MASTER_KEY",
        base64.urlsafe_b64encode(expected).decode("ascii"),
    )

    class ForbiddenKeyring:
        def get_key(self):
            raise AssertionError("environment override must bypass automatic providers")

        def get_existing_key(self):
            raise AssertionError("environment override must bypass automatic providers")

    provider = DefaultKeyProvider(
        "environment-installation",
        state_dir=tmp_path / "must-not-be-created",
        keyring_provider=ForbiddenKeyring(),
    )

    assert provider.get_key() == expected
    assert not provider.state_dir.exists()


@pytest.mark.parametrize(
    "stdin",
    [
        None,
        SimpleNamespace(isatty=lambda: False),
    ],
    ids=["missing", "not-a-tty"],
)
def test_default_key_provider_pins_local_without_probing_keyring_when_headless(
    monkeypatch,
    tmp_path,
    stdin,
):
    monkeypatch.delenv("BRAINHUB_MASTER_KEY", raising=False)
    for variable in ("BRAINHUB_HEADLESS", "CI", "GITHUB_ACTIONS"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setattr(sys, "stdin", stdin)

    def forbidden_keyring_call(*_args):
        raise AssertionError("headless first use must not probe the OS keyring")

    monkeypatch.setitem(
        sys.modules,
        "keyring",
        SimpleNamespace(
            get_password=forbidden_keyring_call,
            set_password=forbidden_keyring_call,
        ),
    )
    provider = DefaultKeyProvider(
        "headless-default-installation",
        state_dir=tmp_path / "keys",
    )

    selected = provider.get_key()

    assert len(selected) == 32
    assert provider.provider_path.read_bytes() == provider.LOCAL_FILE_CHOICE
    assert provider.local_file.key_path.read_bytes() == selected


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("BRAINHUB_HEADLESS", "true"),
        ("CI", "true"),
        ("GITHUB_ACTIONS", "true"),
    ],
)
def test_default_key_provider_pins_local_for_explicit_or_automated_headless_use(
    monkeypatch,
    tmp_path,
    variable,
    value,
):
    monkeypatch.delenv("BRAINHUB_MASTER_KEY", raising=False)
    for candidate in ("BRAINHUB_HEADLESS", "CI", "GITHUB_ACTIONS"):
        monkeypatch.delenv(candidate, raising=False)
    monkeypatch.setenv(variable, value)
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))

    def forbidden_keyring_call(*_args):
        raise AssertionError("automated first use must not probe the OS keyring")

    monkeypatch.setitem(
        sys.modules,
        "keyring",
        SimpleNamespace(
            get_password=forbidden_keyring_call,
            set_password=forbidden_keyring_call,
        ),
    )
    provider = DefaultKeyProvider(
        f"automated-{variable.lower()}-installation",
        state_dir=tmp_path / "keys",
    )

    selected = provider.get_key()

    assert len(selected) == 32
    assert provider.provider_path.read_bytes() == provider.LOCAL_FILE_CHOICE


def test_explicit_headless_false_allows_default_keyring_in_automation(
    monkeypatch,
    tmp_path,
):
    monkeypatch.delenv("BRAINHUB_MASTER_KEY", raising=False)
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("BRAINHUB_HEADLESS", "false")
    monkeypatch.setattr(sys, "stdin", None)
    expected = bytes(range(32))
    monkeypatch.setattr(KeyringKeyProvider, "get_key", lambda _self: expected)

    provider = DefaultKeyProvider(
        "forced-interactive-installation",
        state_dir=tmp_path / "keys",
    )

    assert provider.get_key() == expected
    assert provider.provider_path.read_bytes() == provider.KEYRING_CHOICE
    assert not provider.local_file.key_path.exists()


def test_invalid_headless_override_fails_before_selecting_a_provider(
    monkeypatch,
    tmp_path,
):
    monkeypatch.delenv("BRAINHUB_MASTER_KEY", raising=False)
    monkeypatch.setenv("BRAINHUB_HEADLESS", "sometimes")
    provider = DefaultKeyProvider(
        "invalid-headless-installation",
        state_dir=tmp_path / "keys",
    )

    with pytest.raises(KeyUnavailableError, match="BRAINHUB_HEADLESS"):
        provider.get_key()
    assert not provider.provider_path.exists()
    assert not provider.local_file.key_path.exists()


def test_default_key_provider_pins_working_keyring_and_refuses_later_fallback(
    monkeypatch,
    tmp_path,
):
    monkeypatch.delenv("BRAINHUB_MASTER_KEY", raising=False)
    monkeypatch.setenv("BRAINHUB_HEADLESS", "not-a-boolean")
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: False))
    expected = bytes(range(32))

    class WorkingKeyring:
        def get_key(self):
            return expected

        def get_existing_key(self):
            return expected

    first = DefaultKeyProvider(
        "keyring-installation",
        state_dir=tmp_path / "keys",
        keyring_provider=WorkingKeyring(),
    )
    assert first.get_key() == expected
    assert first.provider_path.read_bytes() == first.KEYRING_CHOICE

    class UnavailableKeyring:
        def get_key(self):
            raise AssertionError("a persisted choice must use the existing-key path")

        def get_existing_key(self):
            raise KeyringUnavailableError("simulated keyring outage")

    reopened = DefaultKeyProvider(
        "keyring-installation",
        state_dir=tmp_path / "keys",
        keyring_provider=UnavailableKeyring(),
    )
    with pytest.raises(KeyringUnavailableError, match="outage"):
        reopened.get_key()
    assert not reopened.local_file.key_path.exists()
    if os.name == "posix":
        assert stat.S_IMODE(first.provider_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(first.state_dir.stat().st_mode) == 0o700


def test_headless_reopen_honors_pinned_default_keyring_and_fails_closed(
    monkeypatch,
    tmp_path,
):
    monkeypatch.delenv("BRAINHUB_MASTER_KEY", raising=False)
    for variable in ("BRAINHUB_HEADLESS", "CI", "GITHUB_ACTIONS"):
        monkeypatch.delenv(variable, raising=False)
    expected = bytes(range(32))
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(KeyringKeyProvider, "get_key", lambda _self: expected)

    first = DefaultKeyProvider(
        "pinned-headless-keyring-installation",
        state_dir=tmp_path / "keys",
    )
    assert first.get_key() == expected
    assert first.provider_path.read_bytes() == first.KEYRING_CHOICE

    def unavailable_existing_key(_self):
        raise KeyringUnavailableError("simulated OS keyring outage")

    monkeypatch.setenv("BRAINHUB_HEADLESS", "not-a-boolean")
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: False))
    monkeypatch.setattr(
        KeyringKeyProvider,
        "get_existing_key",
        unavailable_existing_key,
    )
    reopened = DefaultKeyProvider(
        "pinned-headless-keyring-installation",
        state_dir=tmp_path / "keys",
    )

    with pytest.raises(KeyringUnavailableError, match="outage"):
        reopened.get_key()
    assert not reopened.local_file.key_path.exists()


def test_default_key_provider_falls_back_once_and_keeps_the_same_local_key(
    monkeypatch,
    tmp_path,
):
    monkeypatch.delenv("BRAINHUB_MASTER_KEY", raising=False)

    class UnavailableKeyring:
        def get_key(self):
            raise KeyringUnavailableError("headless Linux")

        def get_existing_key(self):
            raise AssertionError("no keyring choice has been persisted")

    first = DefaultKeyProvider(
        "headless-installation",
        state_dir=tmp_path / "keys",
        keyring_provider=UnavailableKeyring(),
    )
    selected = first.get_key()

    assert len(selected) == 32
    assert first.provider_path.read_bytes() == first.LOCAL_FILE_CHOICE
    assert first.local_file.key_path.read_bytes() == selected
    if os.name == "posix":
        assert stat.S_IMODE(first.provider_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(first.local_file.key_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(first.state_dir.stat().st_mode) == 0o700

    class RecoveredKeyring:
        def get_key(self):
            raise AssertionError("persisted local choice must not probe the keyring")

        def get_existing_key(self):
            raise AssertionError("persisted local choice must not probe the keyring")

    reopened = DefaultKeyProvider(
        "headless-installation",
        state_dir=tmp_path / "keys",
        keyring_provider=RecoveredKeyring(),
    )
    assert reopened.get_key() == selected


def test_concurrent_first_use_creates_one_stable_local_key(monkeypatch, tmp_path):
    monkeypatch.delenv("BRAINHUB_MASTER_KEY", raising=False)

    class UnavailableKeyring:
        def get_key(self):
            raise KeyringUnavailableError("headless Linux")

        def get_existing_key(self):
            raise AssertionError("local provider was selected")

    def load_key(_index):
        return DefaultKeyProvider(
            "concurrent-headless-installation",
            state_dir=tmp_path / "keys",
            keyring_provider=UnavailableKeyring(),
        ).get_key()

    with ThreadPoolExecutor(max_workers=4) as executor:
        keys = list(executor.map(load_key, range(4)))

    assert len(set(keys)) == 1
    assert len(keys[0]) == 32
    assert len(list((tmp_path / "keys").glob("*.key"))) == 1
    assert len(list((tmp_path / "keys").glob("*.provider"))) == 1


def test_default_key_provider_never_replaces_a_missing_selected_local_key(
    monkeypatch,
    tmp_path,
):
    monkeypatch.delenv("BRAINHUB_MASTER_KEY", raising=False)

    class UnavailableKeyring:
        def get_key(self):
            raise KeyringUnavailableError("headless Linux")

        def get_existing_key(self):
            raise AssertionError("local provider was selected")

    first = DefaultKeyProvider(
        "deleted-key-installation",
        state_dir=tmp_path / "keys",
        keyring_provider=UnavailableKeyring(),
    )
    first.get_key()
    first.local_file.key_path.unlink()

    reopened = DefaultKeyProvider(
        "deleted-key-installation",
        state_dir=tmp_path / "keys",
        keyring_provider=UnavailableKeyring(),
    )
    with pytest.raises(KeyUnavailableError, match="missing"):
        reopened.get_key()
    assert not reopened.local_file.key_path.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX ownership and mode checks")
def test_local_key_state_rejects_insecure_mode_and_symbolic_links(
    monkeypatch,
    tmp_path,
):
    monkeypatch.delenv("BRAINHUB_MASTER_KEY", raising=False)

    class UnavailableKeyring:
        def get_key(self):
            raise KeyringUnavailableError("headless Linux")

        def get_existing_key(self):
            raise AssertionError("local provider was selected")

    first = DefaultKeyProvider(
        "tampered-installation",
        state_dir=tmp_path / "keys",
        keyring_provider=UnavailableKeyring(),
    )
    first.get_key()
    first.local_file.key_path.chmod(0o644)

    with pytest.raises(KeyUnavailableError, match="0600"):
        first.get_key()

    first.local_file.key_path.unlink()
    target = tmp_path / "attacker-controlled-key"
    target.write_bytes(bytes(range(32)))
    target.chmod(0o600)
    first.local_file.key_path.symlink_to(target)

    with pytest.raises(KeyUnavailableError, match="symbolic link"):
        first.get_key()

    first.local_file.key_path.unlink()
    first.local_file.key_path.write_bytes(bytes(range(32)))
    first.local_file.key_path.chmod(0o600)
    first.provider_path.chmod(0o644)

    with pytest.raises(KeyUnavailableError, match="0600"):
        first.get_key()

    first.provider_path.unlink()
    first.provider_path.mkdir(mode=0o700)

    with pytest.raises(KeyUnavailableError, match="regular file"):
        first.get_key()


@pytest.mark.skipif(os.name != "posix", reason="POSIX ownership checks")
def test_local_key_state_rejects_files_not_owned_by_current_user(
    monkeypatch,
    tmp_path,
):
    monkeypatch.delenv("BRAINHUB_MASTER_KEY", raising=False)
    state_dir = tmp_path / "keys"
    state_dir.mkdir(mode=0o700)
    actual_uid = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: actual_uid + 1)

    provider = DefaultKeyProvider(
        "wrong-owner-installation",
        state_dir=state_dir,
    )
    with pytest.raises(KeyUnavailableError, match="not owned"):
        provider.get_key()


def test_corrupt_keyring_state_does_not_trigger_local_fallback(monkeypatch, tmp_path):
    monkeypatch.delenv("BRAINHUB_MASTER_KEY", raising=False)

    class CorruptKeyring:
        def get_key(self):
            raise KeyUnavailableError("stored OS keychain master key is corrupt")

        def get_existing_key(self):
            raise AssertionError("provider choice was not persisted")

    provider = DefaultKeyProvider(
        "corrupt-keyring-installation",
        state_dir=tmp_path / "keys",
        keyring_provider=CorruptKeyring(),
    )
    with pytest.raises(KeyUnavailableError, match="corrupt"):
        provider.get_key()
    assert not provider.local_file.key_path.exists()
    assert not provider.provider_path.exists()


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
