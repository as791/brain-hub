"""Executable transport and graph contracts for Brain Hub."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from enum import Enum, StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


EVENT_TYPE_RE = re.compile(r"^com\.brainhub\.[a-z0-9.]+\.v[1-9][0-9]*$")


class NodeType(StrEnum):
    WORKSTREAM = "WORKSTREAM"
    RUN = "RUN"
    TOPIC = "TOPIC"
    TASK = "TASK"
    DECISION = "DECISION"
    ARTIFACT = "ARTIFACT"
    CLAIM = "CLAIM"
    ACTOR = "ACTOR"
    WORKSPACE = "WORKSPACE"


class EdgeType(StrEnum):
    HAS_RUN = "HAS_RUN"
    ABOUT = "ABOUT"
    PRODUCED = "PRODUCED"
    USED = "USED"
    MODIFIES = "MODIFIES"
    DEPENDS_ON = "DEPENDS_ON"
    BLOCKS = "BLOCKS"
    DECIDED_IN = "DECIDED_IN"
    DERIVED_FROM = "DERIVED_FROM"
    REFERENCES = "REFERENCES"
    VERIFIES = "VERIFIES"
    CONTRADICTS = "CONTRADICTS"
    SUPERSEDES = "SUPERSEDES"
    CONTINUES = "CONTINUES"
    ASSERTED_BY = "ASSERTED_BY"
    PARTICIPATES_IN = "PARTICIPATES_IN"


class ConfidenceClass(StrEnum):
    EXTRACTED = "EXTRACTED"
    INFERRED = "INFERRED"
    AMBIGUOUS = "AMBIGUOUS"


class Sensitivity(StrEnum):
    PUBLIC = "PUBLIC"
    INTERNAL = "INTERNAL"
    CONFIDENTIAL = "CONFIDENTIAL"
    RESTRICTED = "RESTRICTED"


class ReviewState(StrEnum):
    UNREVIEWED = "UNREVIEWED"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    NEEDS_REVIEW = "NEEDS_REVIEW"


class EvidenceVisibility(StrEnum):
    LOCAL = "LOCAL"
    SYNCABLE = "SYNCABLE"
    UNAVAILABLE = "UNAVAILABLE"


class EventStatus(StrEnum):
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class CaptureMode(StrEnum):
    HOOK = "hook"
    OTLP = "otlp"
    STREAM = "stream"
    MCP = "mcp"
    MANUAL = "manual"
    IMPORT = "import"


class ContentLevel(StrEnum):
    METADATA = "metadata"
    SUMMARY = "summary"
    CONTENT = "content"


class AgentIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: str = Field(min_length=1, max_length=80)
    surface: str = Field(min_length=1, max_length=80)
    version: str | None = Field(default=None, max_length=80)


class CapturePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: CaptureMode
    content_level: ContentLevel
    redactions: list[str] = Field(default_factory=list, max_length=100)


class EventData(BaseModel):
    """Common event data; extension fields are preserved for future projectors."""

    model_config = ConfigDict(extra="allow")

    agent: AgentIdentity
    workspace_id: str = Field(min_length=1, max_length=256)
    session_id: str = Field(min_length=1, max_length=256)
    turn_id: str | None = Field(default=None, max_length=256)
    parent_session_id: str | None = Field(default=None, max_length=256)
    status: EventStatus
    summary: str | None = Field(default=None, max_length=4000)
    artifacts: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    capture: CapturePolicy


def canonical_json(value: Any) -> bytes:
    """Return stable UTF-8 JSON suitable for hashing and authenticated storage."""

    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", exclude_none=True)

    def json_default(item: Any) -> Any:
        if isinstance(item, BaseModel):
            return item.model_dump(mode="json", exclude_none=True)
        if isinstance(item, datetime):
            return item.isoformat()
        if isinstance(item, Enum):
            return item.value
        raise TypeError(f"{type(item).__name__} is not canonically serializable")

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=json_default,
    ).encode("utf-8")


def sha256_hex(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def stable_id(namespace: str, *parts: Any) -> str:
    digest = sha256_hex([namespace, *parts])
    return f"{namespace}:{digest[:32]}"


def deterministic_event_id(
    *, source: str, event_type: str, subject: str, time: datetime, data: EventData | dict[str, Any]
) -> str:
    return "sha256:" + sha256_hex(
        {
            "source": source,
            "type": event_type,
            "subject": subject,
            "time": time.astimezone(UTC).isoformat(),
            "data": data,
        }
    )


class BrainEvent(BaseModel):
    """CloudEvents 1.0 JSON envelope accepted from every capture adapter."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    specversion: Literal["1.0"] = "1.0"
    id: str = Field(min_length=8, max_length=256)
    source: str = Field(min_length=3, max_length=512)
    type: str = Field(min_length=1, max_length=200)
    subject: str = Field(min_length=1, max_length=512)
    time: datetime
    datacontenttype: Literal["application/json"] = "application/json"
    data: EventData

    @field_validator("source")
    @classmethod
    def source_is_uri(cls, value: str) -> str:
        if ":" not in value or any(ch.isspace() for ch in value):
            raise ValueError("source must be a URI")
        return value

    @field_validator("type")
    @classmethod
    def event_type_is_versioned(cls, value: str) -> str:
        if not EVENT_TYPE_RE.fullmatch(value):
            raise ValueError("event type must be a versioned com.brainhub.* identifier")
        return value

    @classmethod
    def create(
        cls,
        *,
        source: str,
        type: str,
        subject: str,
        data: EventData | dict[str, Any],
        time: datetime | None = None,
    ) -> "BrainEvent":
        when = time or datetime.now(UTC)
        parsed_data = data if isinstance(data, EventData) else EventData.model_validate(data)
        return cls(
            id=deterministic_event_id(
                source=source, event_type=type, subject=subject, time=when, data=parsed_data
            ),
            source=source,
            type=type,
            subject=subject,
            time=when,
            data=parsed_data,
        )


class EvidenceRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_event_id: str = Field(min_length=8, max_length=256)
    locator: str | None = Field(default=None, max_length=512)
    anchor: str | None = Field(default=None, max_length=256)
    content_hash: str | None = Field(default=None, max_length=128)
    visibility: EvidenceVisibility = EvidenceVisibility.LOCAL


class TimeRange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: datetime
    end: datetime | None = None

    @model_validator(mode="after")
    def end_not_before_start(self) -> "TimeRange":
        if self.end is not None and self.end < self.start:
            raise ValueError("time range end must not precede start")
        return self


class Provenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor_id: str | None = Field(default=None, max_length=256)
    extractor: str = Field(default="brainhub-core", min_length=1, max_length=120)
    extractor_version: str = Field(default="0.1.0", min_length=1, max_length=80)
    evidence: list[EvidenceRef] = Field(default_factory=list)


class Node(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=3, max_length=256)
    type: NodeType
    title: str = Field(min_length=1, max_length=300)
    summary: str = Field(default="", max_length=4000)
    content: dict[str, Any] | None = None
    properties: dict[str, Any] = Field(default_factory=dict)
    sensitivity: Sensitivity = Sensitivity.INTERNAL
    review_state: ReviewState = ReviewState.UNREVIEWED
    valid_time: TimeRange
    recorded_time: TimeRange
    provenance: Provenance
    external_ids: list[str] = Field(default_factory=list, max_length=100)
    content_hash: str | None = Field(default=None, max_length=128)
    creation_event_id: str = Field(min_length=8, max_length=256)
    latest_revision_id: str = Field(min_length=8, max_length=256)


class Edge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=3, max_length=256)
    source_id: str = Field(min_length=3, max_length=256)
    target_id: str = Field(min_length=3, max_length=256)
    relation: EdgeType
    explanation: str = Field(min_length=1, max_length=320)
    confidence_class: ConfidenceClass
    confidence_score: float = Field(ge=0, le=1)
    evidence: list[EvidenceRef] = Field(min_length=1)
    valid_time: TimeRange
    recorded_time: TimeRange
    actor_id: str | None = Field(default=None, max_length=256)
    extractor: str = Field(default="brainhub-core", min_length=1, max_length=120)
    extractor_version: str = Field(default="0.1.0", min_length=1, max_length=80)
    sensitivity: Sensitivity = Sensitivity.INTERNAL
    review_state: ReviewState = ReviewState.UNREVIEWED
    properties: dict[str, Any] = Field(default_factory=dict)
    creation_event_id: str = Field(min_length=8, max_length=256)
    latest_revision_id: str = Field(min_length=8, max_length=256)


class SearchResult(BaseModel):
    node: Node
    score: float
    lexical_score: float
    semantic_score: float | None = None
    graph_score: float = 0.0


class SearchResponse(BaseModel):
    results: list[SearchResult]
    search_mode: Literal["hybrid", "lexical_degraded"]
    scope: Literal["global", "anchored"]
    anchor_id: str | None = None
    hops: int | None = None
    projection_version: int
    degraded_reason: str | None = None


class GraphSlice(BaseModel):
    nodes: list[Node]
    edges: list[Edge]
    anchor_id: str | None = None
    hops: int | None = None
    projection_version: int


class PathResponse(BaseModel):
    nodes: list[Node]
    edges: list[Edge]
    projection_version: int


class RecordResponse(BaseModel):
    event_id: str
    sequence: int
    accepted: bool
    projection_version: int


class FeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_id: str = Field(min_length=3, max_length=256)
    verdict: Literal["accept", "reject", "needs_review", "incorrect", "duplicate"]
    note: str | None = Field(default=None, max_length=2000)


class SyncEvent(BaseModel):
    sequence: int = Field(ge=1)
    event_id: str
    event_type: str
    recorded_at: datetime
    canonical_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    graph_payload: dict[str, Any]


class SyncBatch(BaseModel):
    installation_id: UUID
    batch_id: UUID
    first_sequence: int
    last_sequence: int
    events: list[SyncEvent] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def sequences_are_contiguous_and_match_bounds(self) -> "SyncBatch":
        sequences = [event.sequence for event in self.events]
        expected = list(range(self.first_sequence, self.last_sequence + 1))
        if self.last_sequence < self.first_sequence or sequences != expected:
            raise ValueError("sync event sequences must be contiguous and match batch bounds")
        return self
