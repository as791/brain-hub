from __future__ import annotations

import os
from pathlib import Path
import stat
import tempfile
import unittest

from brainhub_adapters.quarantine import BoundedQuarantine


class QuarantineTests(unittest.TestCase):
    def test_count_bound_prunes_oldest_and_preserves_audit_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            quarantine = BoundedQuarantine(directory, max_events=2, max_bytes=100_000)
            paths = []
            for ordinal in range(1, 4):
                result = quarantine.add(
                    {"id": f"evt_{ordinal}", "specversion": "1.0"},
                    http_status=422,
                    original_spool_file=f"source-{ordinal}.json",
                )
                os.utime(result.path, ns=(ordinal, ordinal))
                paths.append(result)

            self.assertEqual(paths[-1].pruned, 1)
            self.assertEqual(quarantine.count(), 2)
            self.assertEqual(
                [record["event_id"] for record in quarantine.records()],
                ["evt_2", "evt_3"],
            )

    def test_byte_bound_refuses_oversize_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            quarantine = BoundedQuarantine(directory, max_events=2, max_bytes=32)
            with self.assertRaises(OSError):
                quarantine.add(
                    {"id": "evt_large", "value": "x" * 100},
                    http_status=413,
                    original_spool_file="source.json",
                )
            self.assertEqual(quarantine.count(), 0)

    def test_directory_and_records_are_permission_restricted(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent) / "quarantine"
            quarantine = BoundedQuarantine(root)
            result = quarantine.add(
                {"id": "evt_permissions", "specversion": "1.0"},
                http_status=400,
                original_spool_file="source.json",
            )
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(result.path.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
