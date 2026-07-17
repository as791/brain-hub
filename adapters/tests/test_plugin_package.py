from __future__ import annotations

import json
from pathlib import Path
import unittest


BRAIN_HUB = Path(__file__).resolve().parents[2]


class PluginPackageTests(unittest.TestCase):
    def test_manifest_and_mcp_are_local_and_non_destructive(self) -> None:
        root = BRAIN_HUB / "plugins" / "brain-hub"
        manifest = json.loads((root / ".codex-plugin" / "plugin.json").read_text())
        mcp = json.loads((root / ".mcp.json").read_text())
        self.assertEqual(manifest["name"], "brain-hub")
        self.assertNotIn("hooks", manifest)
        self.assertNotIn("apps", manifest)
        server = mcp["mcpServers"]["brain-hub"]
        self.assertEqual(server["command"], "/bin/sh")
        self.assertEqual(server["args"][0], "-c")
        self.assertIn(".local/share/brainhub/venv/bin/brainhub", server["args"][1])
        self.assertIn("exec brainhub mcp", server["args"][1])
        self.assertNotIn("url", server)

    def test_repo_marketplace_policy_is_complete(self) -> None:
        marketplace = json.loads(
            (BRAIN_HUB / ".agents" / "plugins" / "marketplace.json").read_text()
        )
        entry = marketplace["plugins"][0]
        self.assertEqual(entry["name"], "brain-hub")
        self.assertEqual(entry["source"], {"source": "local", "path": "./plugins/brain-hub"})
        self.assertIn(entry["policy"]["installation"], {"AVAILABLE", "INSTALLED_BY_DEFAULT"})
        self.assertIn(entry["policy"]["authentication"], {"ON_INSTALL", "ON_USE"})
        self.assertTrue(entry["category"])


if __name__ == "__main__":
    unittest.main()
