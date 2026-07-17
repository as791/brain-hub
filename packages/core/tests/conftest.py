from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
ADAPTER_SRC = ROOT / "adapters" / "src"
if str(ADAPTER_SRC) not in sys.path:
    sys.path.insert(0, str(ADAPTER_SRC))

from brainhub.crypto import ContentCipher, MemoryKeyProvider  # noqa: E402
from brainhub.service import BrainHubService  # noqa: E402
from brainhub.store import EventStore  # noqa: E402


@pytest.fixture
def service(tmp_path: Path):
    store = EventStore(
        tmp_path / "brainhub.db",
        ContentCipher(MemoryKeyProvider(bytes(range(32)))),
    )
    instance = BrainHubService(store, enable_semantic=False)
    try:
        yield instance
    finally:
        instance.close()
