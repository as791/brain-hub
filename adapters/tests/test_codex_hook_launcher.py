from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "plugins" / "brain-hub" / "scripts" / "capture_hook.py"
SPEC = importlib.util.spec_from_file_location("brainhub_capture_hook", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
HOOK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HOOK)


class CodexHookLauncherTests(unittest.TestCase):
    def test_sanitizer_drops_all_content_and_transcript_fields(self) -> None:
        payload = {
            "session_id": "session-1",
            "hook_event_name": "PostToolUse",
            "tool_name": "apply_patch",
            "cwd": "/private/workspace",
            "transcript_path": "/secret/transcript.jsonl",
            "prompt": "private prompt",
            "tool_input": {"patch": "secret"},
            "tool_response": "secret",
            "messages": ["secret"],
            "api_token": "secret",
        }

        safe = HOOK.sanitized_payload(payload)

        self.assertEqual(
            safe,
            {
                "session_id": "session-1",
                "hook_event_name": "PostToolUse",
                "tool_name": "apply_patch",
                "cwd": "/private/workspace",
            },
        )
        serialized = json.dumps(safe)
        self.assertNotIn("secret", serialized)
        self.assertNotIn("transcript", serialized)

    def test_installed_runtime_hook_is_discovered(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plugin = root / "marketplace" / "plugins" / "brain-hub"
            executable = root / "runtime-v1" / "bin" / "brainhub-codex-hook"
            executable.parent.mkdir(parents=True)
            executable.touch()
            (root / "runtime").mkdir()
            (root / "runtime" / "current.json").write_text(
                json.dumps({"venv": str(executable.parents[1])}),
                encoding="utf-8",
            )

            with patch.dict(HOOK.os.environ, {}, clear=True):
                self.assertEqual(HOOK.hook_executable(plugin), str(executable))

    def test_subagent_becomes_child_session_without_forwarding_content(self) -> None:
        safe = HOOK.sanitized_payload(
            {
                "hook_event_name": "SubagentStart",
                "session_id": "parent-session",
                "agent_id": "child-agent",
                "agent_type": "explorer",
                "prompt": "private delegation",
            }
        )

        self.assertEqual(safe["session_id"], "child-agent")
        self.assertEqual(safe["parent_session_id"], "parent-session")
        self.assertEqual(safe["agent_type"], "explorer")
        self.assertNotIn("prompt", safe)

    def test_installed_plugin_never_falls_back_to_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            plugin = Path(temporary) / "marketplace" / "plugins" / "brain-hub"
            plugin.mkdir(parents=True)

            with (
                patch.dict(HOOK.os.environ, {}, clear=True),
                patch.object(HOOK.shutil, "which", return_value="/tmp/untrusted-hook"),
            ):
                self.assertIsNone(HOOK.hook_executable(plugin))

    def test_launcher_forwards_only_sanitized_metadata(self) -> None:
        payload = {
            "hook_event_name": "PostToolUse",
            "session_id": "session-1",
            "tool_name": "apply_patch",
            "prompt": "private prompt",
            "tool_input": {"patch": "private patch"},
            "api_token": "private token",
        }
        with (
            patch.object(HOOK.sys, "stdin", io.StringIO(json.dumps(payload))),
            patch.object(HOOK, "hook_executable", return_value="/managed/hook"),
            patch.object(HOOK.subprocess, "run") as run,
        ):
            self.assertEqual(HOOK.main(), 0)

        run.assert_called_once()
        forwarded = json.loads(run.call_args.kwargs["input"])
        self.assertEqual(
            forwarded,
            {
                "hook_event_name": "PostToolUse",
                "session_id": "session-1",
                "tool_name": "apply_patch",
            },
        )


if __name__ == "__main__":
    unittest.main()
