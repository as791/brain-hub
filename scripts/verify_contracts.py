#!/usr/bin/env python3
"""Portable preflight for schemas, evaluation fixtures, and plugin packaging."""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys
from xml.etree import ElementTree

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "brain-hub"
MARKETPLACE = ROOT / ".agents" / "plugins" / "marketplace.json"
SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?$")


def load_json(path: Path) -> object:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def verify_json_schemas() -> None:
    schema_paths = sorted((ROOT / "schemas").glob("*.schema.json"))
    schema_paths.append(ROOT / "adapters" / "manifests" / "adapter.schema.json")
    for path in schema_paths:
        Draft202012Validator.check_schema(load_json(path))

    adapter_schema = load_json(ROOT / "adapters" / "manifests" / "adapter.schema.json")
    validator = Draft202012Validator(adapter_schema)
    for manifest in sorted((ROOT / "adapters" / "manifests").glob("*.adapter.json")):
        validator.validate(load_json(manifest))


def verify_evaluations() -> None:
    root = ElementTree.parse(ROOT / "evaluations" / "brainhub_mcp.xml").getroot()
    pairs = root.findall("qa_pair")
    require(len(pairs) == 10, "MCP evaluation must contain exactly ten QA pairs")
    for index, pair in enumerate(pairs, start=1):
        question = (pair.findtext("question") or "").strip()
        answer = (pair.findtext("answer") or "").strip()
        require(len(question) >= 40, f"evaluation {index} has an underspecified question")
        require(bool(answer), f"evaluation {index} is missing its stable answer")


def verify_plugin() -> None:
    manifest_path = PLUGIN / ".codex-plugin" / "plugin.json"
    manifest = load_json(manifest_path)
    require(isinstance(manifest, dict), "plugin manifest must be an object")
    require(manifest.get("name") == PLUGIN.name, "plugin name must match its folder")
    require(bool(SEMVER.fullmatch(str(manifest.get("version", "")))), "invalid plugin semver")
    require(bool(manifest.get("description")), "plugin description is required")
    require(bool((manifest.get("author") or {}).get("name")), "plugin author name is required")
    interface = manifest.get("interface") or {}
    for field in ("displayName", "shortDescription", "longDescription", "developerName", "category"):
        require(bool(interface.get(field)), f"plugin interface.{field} is required")

    for field in ("skills", "mcpServers", "apps"):
        value = manifest.get(field)
        if isinstance(value, str):
            require(value.startswith("./"), f"plugin {field} path must start with ./")
            require((PLUGIN / value[2:]).exists(), f"plugin {field} path does not exist")

    serialized = json.dumps(manifest)
    require("[TODO:" not in serialized, "plugin manifest contains a TODO placeholder")
    require("hooks" not in manifest, "hooks is not accepted by the current plugin validator")

    marketplace = load_json(MARKETPLACE)
    require(isinstance(marketplace, dict), "marketplace must be an object")
    entries = [entry for entry in marketplace.get("plugins", []) if entry.get("name") == "brain-hub"]
    require(len(entries) == 1, "marketplace must contain one brain-hub entry")
    entry = entries[0]
    require(entry.get("source") == {"source": "local", "path": "./plugins/brain-hub"}, "invalid plugin source")
    policy = entry.get("policy") or {}
    require(policy.get("installation") in {"AVAILABLE", "INSTALLED_BY_DEFAULT"}, "invalid installation policy")
    require(policy.get("authentication") in {"ON_INSTALL", "ON_USE"}, "invalid auth policy")
    require(bool(entry.get("category")), "marketplace category is required")

    official = (
        Path.home()
        / ".codex"
        / "skills"
        / ".system"
        / "plugin-creator"
        / "scripts"
        / "validate_plugin.py"
    )
    if official.exists():
        subprocess.run([sys.executable, str(official), str(PLUGIN)], check=True)
    else:
        print("official Codex plugin validator not installed; portable checks passed")


def main() -> int:
    verify_json_schemas()
    verify_evaluations()
    verify_plugin()
    print("Brain Hub contract preflight passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
