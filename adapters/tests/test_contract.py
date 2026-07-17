from __future__ import annotations

import json
from pathlib import Path
import unittest

from brainhub_adapters.normalize import normalize_capture


ROOT = Path(__file__).resolve().parents[1]


class ContractTests(unittest.TestCase):
    def fixture(self, path: str) -> dict:
        return json.loads((ROOT / "fixtures" / path).read_text(encoding="utf-8"))

    def test_retry_has_deterministic_id(self) -> None:
        payload = self.fixture("codex/session-complete.json")
        first = normalize_capture("codex", payload)
        second = normalize_capture("codex", payload)
        self.assertEqual(first.id, second.id)
        self.assertEqual(first.time, "1970-01-01T00:00:00Z")
        self.assertEqual(first.to_json(), second.to_json())
        self.assertEqual(first.type, "com.brainhub.agent.run.completed.v1")
        self.assertRegex(first.type, r"^com\.brainhub\.[a-z0-9.]+\.v[1-9][0-9]*$")

    def test_invalid_host_time_uses_stable_unknown_sentinel(self) -> None:
        payload = self.fixture("cursor/stop.json")
        payload["timestamp"] = "not-a-date"
        first = normalize_capture("cursor", payload)
        second = normalize_capture("cursor", payload)
        self.assertEqual(first.time, "1970-01-01T00:00:00Z")
        self.assertEqual(first.to_json(), second.to_json())

    def test_transcript_and_absolute_paths_are_not_serialized(self) -> None:
        payload = self.fixture("claude-code/session-end.json")
        serialized = normalize_capture("claude", payload).to_json()
        self.assertNotIn("transcript.jsonl", serialized)
        self.assertNotIn("/private/", serialized)
        self.assertNotIn("transcript_path", serialized)

    def test_all_profiles_have_required_contract(self) -> None:
        fixtures = {
            "codex": "codex/session-complete.json",
            "claude": "claude-code/session-end.json",
            "cursor": "cursor/stop.json",
            "antigravity": "antigravity/run-complete.json",
        }
        for agent, fixture in fixtures.items():
            with self.subTest(agent=agent):
                event = normalize_capture(agent, self.fixture(fixture)).as_dict()
                self.assertEqual(event["specversion"], "1.0")
                self.assertEqual(event["datacontenttype"], "application/json")
                self.assertIn(event["data"]["status"], {"completed", "unknown"})
                self.assertFalse(event["data"]["capture"].get("content"))

    def test_secret_patterns_are_redacted_from_explicit_summary(self) -> None:
        event = normalize_capture(
            "codex",
            {
                "event": "stop",
                "thread_id": "t-1",
                "summary": "Used token=super-secret-value safely",
            },
        )
        self.assertIn("[REDACTED]", event.data["summary"])
        self.assertNotIn("super-secret-value", event.to_json())

    def test_cloud_keys_jwts_and_private_keys_are_redacted(self) -> None:
        samples = (
            "AWS " + "AKIA" + "ABCDEFGHIJKLMNOP was rotated",
            "session " + "eyJ" + "abcdefghijk.abcdefghijk.abcdefghij expired",
            "-----BEGIN "
            + "PRIVATE KEY-----\nsecret-material\n-----END PRIVATE KEY-----",
            "api_" + "key=plain-development-secret",
        )
        for index, summary in enumerate(samples):
            with self.subTest(index=index):
                event = normalize_capture(
                    "codex",
                    {
                        "event": "stop",
                        "event_id": f"secret-{index}",
                        "thread_id": "t-1",
                        "summary": summary,
                    },
                )
                self.assertIn("[REDACTED]", event.data["summary"])

    def test_standalone_service_tokens_are_redacted_before_spooling(self) -> None:
        samples = (
            "gho_" + "abcdefghijk12345",
            "ghu_" + "abcdefghijk12345",
            "ghs_" + "abcdefghijk12345",
            "ghr_" + "abcdefghijk12345",
            "npm_" + "abcdefghijk12345",
            "hf_" + "abcdefghijk12345",
            "glpat-" + "abcdefghijk12345",
        )
        for index, token in enumerate(samples):
            with self.subTest(token=token.split("_")[0]):
                event = normalize_capture(
                    "codex",
                    {
                        "event": "stop",
                        "event_id": f"service-token-{index}",
                        "thread_id": "t-service-token",
                        "summary": f"rotated {token} successfully",
                    },
                )
                self.assertIn("[REDACTED]", event.data["summary"])
                self.assertNotIn(token, event.to_json())

    def test_absolute_paths_are_redacted_inside_explicit_summaries(self) -> None:
        event = normalize_capture(
            "codex",
            {
                "event": "stop",
                "event_id": "summary-paths",
                "thread_id": "t-summary-paths",
                "summary": (
                    "Edited /Users/alice/private/secret.py and "
                    r"C:\\Users\\alice\\private\\secret.py; see https://example.com/docs"
                ),
            },
        )
        serialized = event.to_json()
        self.assertIn("[REDACTED_PATH]", event.data["summary"])
        self.assertNotIn("/Users/alice", serialized)
        self.assertNotIn(r"C:\\Users\\alice", serialized)
        self.assertIn("https://example.com/docs", serialized)

    def test_source_event_is_sanitized_before_the_event_is_spooled(self) -> None:
        token = "gho_" + "abcdefghijk12345"
        event = normalize_capture(
            "codex",
            {
                "event": f"/srv/private/hooks/{token}",
                "event_id": "sanitized-source-event",
                "thread_id": "t-source-event",
            },
        )
        serialized = event.to_json()
        self.assertEqual(event.data["metadata"]["source_event"], "redacted_path")
        self.assertNotIn("/srv/private", serialized)
        self.assertNotIn(token, serialized)

    def test_sensitive_artifact_name_is_not_serialized(self) -> None:
        event = normalize_capture(
            "cursor",
            {
                "event_name": "stop",
                "conversation_id": "c-sensitive",
                "files": ["/project/.env", "/project/src/app.py"],
            },
        )
        serialized = event.to_json()
        self.assertNotIn(".env", serialized)
        self.assertIn("app.py", serialized)

    def test_missing_session_does_not_hash_prompt_content(self) -> None:
        common = {"event": "stop", "event_id": "same-event", "cwd": "/same/workspace"}
        first = normalize_capture("codex", {**common, "prompt": "first secret"})
        second = normalize_capture("codex", {**common, "prompt": "second secret"})
        self.assertEqual(first.data["session_id"], second.data["session_id"])

    def test_otlp_attributes_are_normalized_without_body(self) -> None:
        event = normalize_capture(
            "codex",
            {
                "attributes": [
                    {"key": "event.name", "value": {"stringValue": "run.completed"}},
                    {"key": "session.id", "value": {"stringValue": "otel-session"}},
                    {"key": "brainhub.summary", "value": {"stringValue": "Indexed the graph."}},
                ],
                "body": {"stringValue": "raw output must stay out"},
                "timestamp": "not-a-date",
            },
            mode="otlp",
        )
        self.assertEqual(event.data["status"], "completed")
        self.assertEqual(event.data["summary"], "Indexed the graph.")
        self.assertNotIn("raw output", event.to_json())
        self.assertTrue(event.time.endswith("Z"))


if __name__ == "__main__":
    unittest.main()
