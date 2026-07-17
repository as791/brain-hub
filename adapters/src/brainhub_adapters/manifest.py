"""Tiny dependency-free SDK for registering third-party agent adapters."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any


_ID = re.compile(r"^[a-z0-9][a-z0-9.-]{1,79}$")
_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_INPUTS = {"hook", "otlp", "stream", "mcp", "manual", "import"}


@dataclass(frozen=True, slots=True)
class AdapterManifest:
    schema_version: str
    id: str
    display_name: str
    version: str
    command: tuple[str, ...]
    delivery_command: tuple[str, ...]
    delivery_token_env: str
    delivery_supervision: str
    inputs: tuple[str, ...]
    default_content_level: str
    reads_transcripts: bool
    absolute_paths: str


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def parse_manifest(payload: Any) -> AdapterManifest:
    _require(isinstance(payload, dict), "adapter manifest must be a JSON object")
    allowed = {
        "schema_version",
        "id",
        "display_name",
        "version",
        "command",
        "delivery",
        "inputs",
        "privacy",
    }
    _require(not set(payload) - allowed, "adapter manifest contains unknown fields")
    _require(payload.get("schema_version") == "1", "unsupported adapter schema_version")
    adapter_id = payload.get("id")
    _require(isinstance(adapter_id, str) and bool(_ID.fullmatch(adapter_id)), "invalid adapter id")
    display_name = payload.get("display_name")
    _require(isinstance(display_name, str) and bool(display_name.strip()), "invalid display_name")
    version = payload.get("version")
    _require(isinstance(version, str) and bool(_VERSION.fullmatch(version)), "invalid version")
    command = payload.get("command")
    _require(
        isinstance(command, list)
        and bool(command)
        and all(isinstance(part, str) and bool(part.strip()) for part in command),
        "command must be a non-empty string array",
    )
    delivery = payload.get("delivery")
    _require(isinstance(delivery, dict), "delivery must be an object")
    _require(
        set(delivery) == {"command", "token_env", "supervision"},
        "delivery must declare the complete v1 contract",
    )
    delivery_command = delivery.get("command")
    _require(
        isinstance(delivery_command, list)
        and len(delivery_command) >= 2
        and all(
            isinstance(part, str) and bool(part.strip()) for part in delivery_command
        ),
        "delivery command must be a string array",
    )
    _require(
        delivery.get("token_env") == "BRAINHUB_API_TOKEN",
        "delivery token_env must be BRAINHUB_API_TOKEN",
    )
    _require(
        delivery.get("supervision") == "foreground",
        "delivery supervision must be foreground",
    )
    inputs = payload.get("inputs")
    _require(
        isinstance(inputs, list)
        and bool(inputs)
        and all(value in _INPUTS for value in inputs)
        and len(inputs) == len(set(inputs)),
        "inputs contain unsupported or duplicate modes",
    )
    privacy = payload.get("privacy")
    _require(isinstance(privacy, dict), "privacy must be an object")
    _require(
        set(privacy) == {"default_content_level", "reads_transcripts", "absolute_paths"},
        "privacy must declare the complete v1 contract",
    )
    _require(
        privacy.get("default_content_level") in {"metadata", "summary"},
        "invalid default_content_level",
    )
    _require(privacy.get("reads_transcripts") is False, "v1 adapters may not read transcripts")
    _require(privacy.get("absolute_paths") == "hash", "v1 adapters must hash absolute paths")
    return AdapterManifest(
        schema_version="1",
        id=adapter_id,
        display_name=display_name.strip(),
        version=version,
        command=tuple(command),
        delivery_command=tuple(delivery_command),
        delivery_token_env="BRAINHUB_API_TOKEN",
        delivery_supervision="foreground",
        inputs=tuple(inputs),
        default_content_level=privacy["default_content_level"],
        reads_transcripts=False,
        absolute_paths="hash",
    )


def load_manifest(path: str | Path) -> AdapterManifest:
    return parse_manifest(json.loads(Path(path).read_text(encoding="utf-8")))
