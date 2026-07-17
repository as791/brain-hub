from __future__ import annotations

import io
import json
import tempfile
import unittest

from brainhub_adapters.hook import capture_stream
from brainhub_adapters.spool import BoundedSpool


class SpoolTests(unittest.TestCase):
    def test_deduplicates_and_bounds_event_count(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            spool = BoundedSpool(directory, max_events=2, max_bytes=100_000)
            first = spool.enqueue({"id": "one", "value": 1})
            duplicate = spool.enqueue({"id": "one", "value": 1})
            spool.enqueue({"id": "two", "value": 2})
            last = spool.enqueue({"id": "three", "value": 3})
            self.assertEqual(first.state, "queued")
            self.assertEqual(duplicate.state, "duplicate")
            self.assertEqual(last.pruned, 1)
            self.assertEqual(len(list(spool.pending())), 2)

    def test_oversize_is_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            spool = BoundedSpool(directory, max_events=2, max_bytes=32)
            result = spool.enqueue({"id": "large", "value": "x" * 100})
            self.assertEqual(result.state, "dropped-oversize")

    def test_opaque_ids_do_not_alias_and_changed_content_is_a_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            spool = BoundedSpool(directory, max_events=10, max_bytes=100_000)
            first = spool.enqueue({"id": "run:a/b", "value": 1})
            distinct = spool.enqueue({"id": "runab", "value": 1})
            conflict = spool.enqueue({"id": "run:a/b", "value": 2})

            self.assertEqual(first.state, "queued")
            self.assertEqual(distinct.state, "queued")
            self.assertEqual(conflict.state, "conflict")
            self.assertEqual(len(list(spool.pending())), 2)

    def test_hook_only_queues(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            spool = BoundedSpool(directory)
            state = capture_stream(
                "cursor",
                io.StringIO(
                    json.dumps(
                        {"event_name": "stop", "conversation_id": "c-1", "summary": "Done"}
                    )
                ),
                spool=spool,
            )
            self.assertEqual(state, "queued")
            self.assertEqual(len(list(spool.pending())), 1)


if __name__ == "__main__":
    unittest.main()
