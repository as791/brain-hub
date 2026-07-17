from __future__ import annotations

import json
from pathlib import Path
import unittest

from brainhub_adapters.manifest import load_manifest


ROOT = Path(__file__).resolve().parents[1]


class ManifestTests(unittest.TestCase):
    def test_every_manifest_is_private_by_default(self) -> None:
        manifests = sorted((ROOT / "manifests").glob("*.adapter.json"))
        self.assertEqual(len(manifests), 5)
        for path in manifests:
            with self.subTest(path=path.name):
                payload = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(payload["schema_version"], "1")
                self.assertFalse(payload["privacy"]["reads_transcripts"])
                self.assertEqual(payload["privacy"]["absolute_paths"], "hash")
                self.assertIn(payload["privacy"]["default_content_level"], {"metadata", "summary"})
                manifest = load_manifest(path)
                self.assertEqual(manifest.id, payload["id"])
                self.assertEqual(manifest.delivery_command, ("brainhub-adapter", "watch"))
                self.assertEqual(manifest.delivery_token_env, "BRAINHUB_API_TOKEN")
                self.assertEqual(manifest.delivery_supervision, "foreground")


if __name__ == "__main__":
    unittest.main()
