from __future__ import annotations

import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from brainhub_adapters.normalize import normalize_capture
from brainhub_adapters.spool import BoundedSpool

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

        self.assertEqual(safe["session_id"], "session-1")
        self.assertEqual(safe["hook_event_name"], "PostToolUse")
        self.assertEqual(safe["tool_name"], "apply_patch")
        self.assertEqual(safe["cwd"], "/private/workspace")
        self.assertRegex(safe["capture_id"], r"^cap_[0-9a-f]{40}$")
        serialized = json.dumps(safe)
        self.assertNotIn("secret", serialized)
        self.assertNotIn("transcript", serialized)

    def test_identical_calls_get_distinct_capture_ids_and_replay_is_stable(self) -> None:
        payload = {
            "client_id": "codex-cli",
            "session_id": "session-1",
            "turn_id": "turn-1",
            "hook_event_name": "PostToolUse",
            "tool_name": "read_file",
            "tool_input": {"command": "cat private.txt"},
            "tool_response": "private output",
            "headers": {"Authorization": "secret"},
            "error": "free-form private error",
        }
        with patch.object(HOOK.time, "monotonic_ns", side_effect=(101, 102, 103)):
            captures = [HOOK.sanitized_payload(payload) for _ in range(3)]

        self.assertEqual(len({capture["capture_id"] for capture in captures}), 3)
        self.assertEqual(HOOK._capture_identity(captures[0], 101), captures[0]["capture_id"])
        with tempfile.TemporaryDirectory() as temporary:
            spool = BoundedSpool(temporary)
            events = [normalize_capture("codex", capture).as_dict() for capture in captures]
            self.assertEqual([spool.enqueue(event).state for event in events], ["queued"] * 3)
            self.assertEqual(spool.enqueue(events[0]).state, "duplicate")
            self.assertEqual(len(list(spool.pending())), 3)
        serialized = json.dumps(captures)
        for sensitive in ("cat private.txt", "private output", "Authorization", "private error"):
            self.assertNotIn(sensitive, serialized)

    def test_native_invocation_id_is_preserved_without_fallback_identity(self) -> None:
        safe = HOOK.sanitized_payload(
            {
                "session_id": "session-1",
                "turn_id": "turn-1",
                "hook_event_name": "PostToolUse",
                "tool_use_id": "native-call-1",
            }
        )

        self.assertEqual(safe["tool_use_id"], "native-call-1")
        self.assertNotIn("capture_id", safe)
        self.assertNotIn("capture_sequence", safe)

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
        self.assertEqual(forwarded["hook_event_name"], "PostToolUse")
        self.assertEqual(forwarded["session_id"], "session-1")
        self.assertEqual(forwarded["tool_name"], "apply_patch")
        self.assertIn("capture_id", forwarded)
        self.assertIn("observed_at", forwarded)


if __name__ == "__main__":
    unittest.main()
