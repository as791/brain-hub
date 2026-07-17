"""Data minimization helpers used before anything reaches disk."""

from __future__ import annotations

from pathlib import PurePath
import re
from typing import Any, Iterable, Mapping

from .model import stable_digest


_FORBIDDEN_KEY_PARTS = {
    "authorization",
    "cookie",
    "credential",
    "input",
    "message",
    "output",
    "password",
    "prompt",
    "raw",
    "secret",
    "token",
    "transcript",
}
_SECRET_PATTERNS = (
    re.compile(r"\b(?:sk|rk|pk|ghp|github_pat)[_-][A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\b(?:gho|ghu|ghs|ghr|npm|hf)_[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}\b", re.IGNORECASE),
    re.compile(r"\b(?:api[_-]?key|password|secret|token)\s*[:=]\s*\S+", re.IGNORECASE),
    re.compile(
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
        re.DOTALL,
    ),
)
_ABSOLUTE_PATH_PATTERNS = (
    re.compile(r"(?<![\w.:/])/(?:[^/\s]+/)+[^\s,;]+"),
    re.compile(r"(?i)(?<![\w])(?:[a-z]:\\|\\\\)[^\s,;]+"),
)
_SENSITIVE_FILE_NAMES = {
    ".env",
    ".env.local",
    ".npmrc",
    ".pypirc",
    "credentials",
    "credentials.json",
    "id_dsa",
    "id_ed25519",
    "id_rsa",
}


def safe_text(value: Any, *, limit: int = 4000) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    for pattern in _SECRET_PATTERNS:
        value = pattern.sub("[REDACTED]", value)
    for pattern in _ABSOLUTE_PATH_PATTERNS:
        value = pattern.sub("[REDACTED_PATH]", value)
    return value[:limit]


def explicit_summary(payload: Mapping[str, Any]) -> str | None:
    """Read only fields which explicitly claim to contain a summary."""

    for key in ("brainhub_summary", "summary", "semantic_summary"):
        summary = safe_text(payload.get(key))
        if summary:
            return summary
    return None


def opaque_workspace(value: Any) -> str:
    if isinstance(value, str) and value.startswith("ws_") and len(value) <= 256:
        return value
    raw = str(value or "global")
    return f"ws_{stable_digest(raw)[:24]}"


def opaque_reference(value: Any, *, prefix: str = "ref") -> str:
    return f"{prefix}_{stable_digest(str(value))[:32]}"


def _name_only(value: str) -> str | None:
    try:
        name = PurePath(value).name
    except (TypeError, ValueError):
        return None
    if name.lower() in _SENSITIVE_FILE_NAMES:
        return None
    return safe_text(name, limit=180)


def artifact_references(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Convert artifact/file hints to opaque citations, never absolute paths."""

    candidates: list[Any] = []
    for key in ("artifacts", "changed_files", "files"):
        value = payload.get(key)
        if isinstance(value, list):
            candidates.extend(value[:100])

    artifacts: list[dict[str, Any]] = []
    for item in candidates[:100]:
        if isinstance(item, Mapping):
            raw_ref = item.get("uri") or item.get("path") or item.get("id")
            kind = safe_text(item.get("kind"), limit=60) or "artifact"
            label = safe_text(item.get("name"), limit=180)
        else:
            raw_ref = item
            kind = "file"
            label = _name_only(str(item))
        if raw_ref is None:
            continue
        record: dict[str, Any] = {
            "id": opaque_reference(raw_ref, prefix="artifact"),
            "kind": kind,
        }
        if label:
            record["name"] = label
        artifacts.append(record)
    return artifacts


def safe_metadata(payload: Mapping[str, Any], keys: Iterable[str]) -> dict[str, Any]:
    """Copy a scalar allowlist after denying content- and secret-shaped keys."""

    result: dict[str, Any] = {}
    for key in keys:
        normalized = key.lower().replace("-", "_")
        if any(part in normalized for part in _FORBIDDEN_KEY_PARTS):
            continue
        value = payload.get(key)
        if isinstance(value, bool) or isinstance(value, int) or isinstance(value, float):
            result[key] = value
        elif isinstance(value, str):
            text = safe_text(value, limit=500)
            if text:
                result[key] = text
    return result
