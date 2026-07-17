from __future__ import annotations

from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import threading
import tempfile
import unittest
from unittest import mock

from brainhub_adapters.deliver import flush_spool
from brainhub_adapters.quarantine import BoundedQuarantine
from brainhub_adapters.spool import BoundedSpool


class _Handler(BaseHTTPRequestHandler):
    status = 202
    statuses: dict[str, int] = {}
    received: list[tuple[str | None, str | None, dict]] = []

    def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        type(self).received.append(
            (
                self.headers.get("Idempotency-Key"),
                self.headers.get("Authorization"),
                payload,
            )
        )
        self.send_response(type(self).statuses.get(str(payload.get("id")), type(self).status))
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return


class DeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        _Handler.received = []
        _Handler.status = 202
        _Handler.statuses = {}
        self.server = HTTPServer(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)

    def endpoint(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}/v1/events"

    def test_accepted_event_is_acknowledged_with_exact_idempotency_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            spool = BoundedSpool(directory)
            spool.enqueue({"id": "evt_exact", "specversion": "1.0"})
            with mock.patch.dict(os.environ, {"BRAINHUB_API_TOKEN": ""}):
                result = flush_spool(spool, endpoint=self.endpoint())
            self.assertEqual(result.delivered, 1)
            self.assertEqual(result.remaining, 0)
            self.assertEqual(_Handler.received[0][0], "evt_exact")
            self.assertIsNone(_Handler.received[0][1])
            self.assertEqual(_Handler.received[0][2]["id"], "evt_exact")

    def test_bearer_token_is_read_from_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            spool = BoundedSpool(directory)
            spool.enqueue({"id": "evt_auth", "specversion": "1.0"})
            with mock.patch.dict(os.environ, {"BRAINHUB_API_TOKEN": "local-secret"}):
                result = flush_spool(spool, endpoint=self.endpoint())
            self.assertEqual(result.delivered, 1)
            self.assertEqual(_Handler.received[0][1], "Bearer local-secret")

    def test_explicit_bearer_token_overrides_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            spool = BoundedSpool(directory)
            spool.enqueue({"id": "evt_explicit_auth", "specversion": "1.0"})
            with mock.patch.dict(os.environ, {"BRAINHUB_API_TOKEN": "environment-secret"}):
                result = flush_spool(
                    spool,
                    endpoint=self.endpoint(),
                    api_token="explicit-secret",
                )
            self.assertEqual(result.delivered, 1)
            self.assertEqual(_Handler.received[0][1], "Bearer explicit-secret")

    def test_conflict_moves_to_quarantine_for_review(self) -> None:
        _Handler.status = 409
        with tempfile.TemporaryDirectory() as directory:
            spool = BoundedSpool(directory)
            spool.enqueue({"id": "evt_conflict", "specversion": "1.0"})
            result = flush_spool(spool, endpoint=self.endpoint())
            self.assertEqual(result.delivered, 0)
            self.assertEqual(result.remaining, 0)
            self.assertEqual(result.quarantined, 1)
            self.assertIsNone(result.error)
            self.assertEqual(result.quarantine_path, str(Path(directory) / "quarantine"))
            records = list(BoundedQuarantine(Path(directory) / "quarantine").records())
            self.assertEqual(records[0]["event_id"], "evt_conflict")
            self.assertEqual(records[0]["reason"]["http_status"], 409)

    def test_permanent_failures_do_not_block_later_events(self) -> None:
        statuses = {400, 409, 413, 422}
        with tempfile.TemporaryDirectory() as directory:
            spool = BoundedSpool(directory)
            event_ids = [f"evt_{status}" for status in sorted(statuses)] + ["evt_ok"]
            for ordinal, event_id in enumerate(event_ids, start=1):
                queued = spool.enqueue({"id": event_id, "specversion": "1.0"})
                assert queued.path is not None
                os.utime(queued.path, ns=(ordinal, ordinal))
            _Handler.statuses = {f"evt_{status}": status for status in statuses}

            result = flush_spool(spool, endpoint=self.endpoint())

            self.assertEqual(result.delivered, 1)
            self.assertEqual(result.quarantined, 4)
            self.assertEqual(result.remaining, 0)
            self.assertIsNone(result.error)
            self.assertEqual([item[2]["id"] for item in _Handler.received], event_ids)
            records = list(BoundedQuarantine(Path(directory) / "quarantine").records())
            self.assertEqual(
                {record["reason"]["http_status"] for record in records},
                statuses,
            )

    def test_transient_failures_remain_queued_and_stop_the_drain(self) -> None:
        for status in (401, 403, 429, 500):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                _Handler.received = []
                _Handler.statuses = {"evt_retry": status}
                spool = BoundedSpool(directory)
                first = spool.enqueue({"id": "evt_retry", "specversion": "1.0"})
                second = spool.enqueue({"id": "evt_later", "specversion": "1.0"})
                assert first.path is not None and second.path is not None
                os.utime(first.path, ns=(1, 1))
                os.utime(second.path, ns=(2, 2))

                result = flush_spool(spool, endpoint=self.endpoint())

                self.assertEqual(result.delivered, 0)
                self.assertEqual(result.quarantined, 0)
                self.assertEqual(result.remaining, 2)
                self.assertEqual(result.error, f"Brain Hub returned HTTP {status}")
                self.assertEqual([item[2]["id"] for item in _Handler.received], ["evt_retry"])


if __name__ == "__main__":
    unittest.main()
