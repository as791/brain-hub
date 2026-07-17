"""Loopback-first FastAPI surface for the UI, SDKs, and adapters."""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Annotated, Literal

from fastapi import (
    BackgroundTasks,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from .graph import EvidenceGraph, GraphBoundsError, GraphNotFoundError
from .models import BrainEvent, FeedbackRequest, NodeType
from .policy import CapturePolicyError
from .service import BrainHubService
from .store import EventIntegrityError, ProjectionIntegrityError


PRODUCT_ID = "brainhub"
PRODUCT_VERSION = "0.1.0"


@dataclass(slots=True)
class ApiSettings:
    token: str | None = None
    instance_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    control_token: str | None = None
    shutdown_callback: Callable[[], None] | None = None
    max_content_length: int = 2 * 1024 * 1024
    websocket_poll_interval_seconds: float = 1.0
    allowed_origins: tuple[str, ...] = (
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        "http://127.0.0.1:4173",
        "http://localhost:4173",
        "http://127.0.0.1:8420",
        "http://localhost:8420",
    )


class SearchFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kinds: list[NodeType] = Field(default_factory=list, max_length=9)

    @field_validator("kinds", mode="before")
    @classmethod
    def normalize_node_kinds(cls, value):
        return [str(item).upper() for item in (value or [])]


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=1000)
    anchor_id: str | None = Field(default=None, max_length=256)
    hops: int = Field(default=2, ge=0, le=EvidenceGraph.MAX_HOPS)
    limit: int = Field(default=20, ge=1, le=100)
    scope: Literal["anchored", "global"] = "anchored"
    valid_at: AwareDatetime | None = None
    filters: SearchFilters | None = None


class PathRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(min_length=3, max_length=256)
    target_id: str = Field(min_length=3, max_length=256)
    directed: bool = False
    max_length: int = Field(default=8, ge=1, le=EvidenceGraph.MAX_PATH_LENGTH)
    valid_at: AwareDatetime | None = None


class SyncAckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    last_sequence: int = Field(ge=1)


def _authorized(value: str | None, expected: str | None) -> bool:
    if expected is None:
        return True
    if not value or not value.startswith("Bearer "):
        return False
    return hmac.compare_digest(value.removeprefix("Bearer "), expected)


def _is_loopback(request: Request) -> bool:
    if request.client is None:
        return False
    try:
        return ipaddress.ip_address(request.client.host).is_loopback
    except ValueError:
        return request.client.host.casefold() == "localhost"


def create_app(
    service: BrainHubService,
    *,
    settings: ApiSettings | None = None,
) -> FastAPI:
    config = settings or ApiSettings()
    app = FastAPI(
        title="Brain Hub",
        version=PRODUCT_VERSION,
        description="Local-first evidence-backed memory graph for AI agents.",
    )
    app.state.brainhub = service
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(config.allowed_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "Idempotency-Key",
            "X-BrainHub-Control",
        ],
    )

    @app.middleware("http")
    async def policy_boundary(request: Request, call_next):
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                parsed_length = int(content_length)
            except ValueError:
                return JSONResponse(status_code=400, content={"detail": "invalid Content-Length"})
            if parsed_length < 0:
                return JSONResponse(status_code=400, content={"detail": "invalid Content-Length"})
            if parsed_length > config.max_content_length:
                return JSONResponse(status_code=413, content={"detail": "request body is too large"})
        if request.method in {"POST", "PUT", "PATCH"}:
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > config.max_content_length:
                    return JSONResponse(
                        status_code=413, content={"detail": "request body is too large"}
                    )
            request._body = bytes(body)
        if request.method == "OPTIONS" or request.url.path in {
            "/healthz",
            "/_brainhub/control/shutdown",
        }:
            return await call_next(request)
        if not _authorized(request.headers.get("authorization"), config.token):
            return JSONResponse(status_code=401, content={"detail": "invalid bearer token"})
        return await call_next(request)

    @app.exception_handler(EventIntegrityError)
    async def event_conflict(_request: Request, exc: EventIntegrityError):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(ProjectionIntegrityError)
    async def projection_conflict(_request: Request, exc: ProjectionIntegrityError):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(CapturePolicyError)
    async def capture_policy_failure(_request: Request, exc: CapturePolicyError):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(GraphNotFoundError)
    async def graph_missing(_request: Request, exc: GraphNotFoundError):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(GraphBoundsError)
    async def graph_bounds(_request: Request, exc: GraphBoundsError):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.get("/healthz")
    def health() -> dict[str, object]:
        return {
            "instance_id": config.instance_id,
            "product": PRODUCT_ID,
            "status": "ok",
            "version": PRODUCT_VERSION,
        }

    @app.post("/_brainhub/control/shutdown", include_in_schema=False, status_code=202)
    def controlled_shutdown(
        request: Request,
        background_tasks: BackgroundTasks,
        supplied_token: Annotated[
            str | None,
            Header(alias="X-BrainHub-Control"),
        ] = None,
    ) -> dict[str, object]:
        if (
            not _is_loopback(request)
            or config.control_token is None
            or supplied_token is None
            or not hmac.compare_digest(supplied_token, config.control_token)
            or config.shutdown_callback is None
        ):
            raise HTTPException(status_code=403, detail="control request denied")
        background_tasks.add_task(config.shutdown_callback)
        return {
            "instance_id": config.instance_id,
            "product": PRODUCT_ID,
            "status": "stopping",
        }

    @app.post("/v1/events")
    def record_event(
        event: BrainEvent,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ):
        if idempotency_key is not None and not hmac.compare_digest(idempotency_key, event.id):
            raise HTTPException(
                status_code=400,
                detail="Idempotency-Key must equal the CloudEvents event.id",
            )
        response = service.record(event)
        return JSONResponse(
            status_code=201 if response.accepted else 200,
            content=response.model_dump(mode="json"),
        )

    @app.get("/v1/graph")
    def graph_snapshot(
        node_limit: Annotated[int, Query(ge=1, le=2_000)] = 2_000,
        edge_limit: Annotated[int, Query(ge=0, le=10_000)] = 10_000,
        valid_at: AwareDatetime | None = None,
    ):
        return service.get_graph(
            node_limit=node_limit, edge_limit=edge_limit, valid_at=valid_at
        )

    @app.post("/v1/search")
    def search_post(body: SearchRequest):
        if body.scope == "anchored" and body.anchor_id is None:
            raise HTTPException(status_code=422, detail="anchored search requires anchor_id")
        return service.search(
            body.query,
            anchor_id=body.anchor_id,
            hops=body.hops,
            limit=body.limit,
            global_scope=body.scope == "global",
            valid_at=body.valid_at,
            node_types=set(body.filters.kinds) if body.filters and body.filters.kinds else None,
        )

    @app.get("/v1/search")
    def search_get(
        query: Annotated[str, Query(min_length=1, max_length=1000)],
        anchor_id: Annotated[str | None, Query(max_length=256)] = None,
        hops: Annotated[int, Query(ge=0, le=EvidenceGraph.MAX_HOPS)] = 2,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        scope: Literal["anchored", "global"] = "anchored",
        valid_at: AwareDatetime | None = None,
        node_type: Annotated[list[NodeType] | None, Query()] = None,
    ):
        if scope == "anchored" and anchor_id is None:
            raise HTTPException(status_code=422, detail="anchored search requires anchor_id")
        return service.search(
            query,
            anchor_id=anchor_id,
            hops=hops,
            limit=limit,
            global_scope=scope == "global",
            valid_at=valid_at,
            node_types=set(node_type) if node_type else None,
        )

    @app.get("/v1/nodes/{node_id}")
    def get_node(node_id: str):
        node = service.get_node(node_id)
        if node is None:
            raise HTTPException(status_code=404, detail="node not found")
        return node

    @app.get("/v1/nodes/{node_id}/expand")
    def expand_node(
        node_id: str,
        hops: Annotated[int, Query(ge=0, le=EvidenceGraph.MAX_HOPS)] = 1,
        relation: Annotated[list[str] | None, Query()] = None,
        node_limit: Annotated[int, Query(ge=1, le=2_000)] = 2_000,
        edge_limit: Annotated[int, Query(ge=0, le=10_000)] = 10_000,
        valid_at: AwareDatetime | None = None,
    ):
        return service.expand(
            node_id,
            hops=hops,
            relation_types=relation,
            node_limit=node_limit,
            edge_limit=edge_limit,
            valid_at=valid_at,
        )

    @app.post("/v1/path")
    def path(body: PathRequest):
        return service.path(
            body.source_id,
            body.target_id,
            directed=body.directed,
            max_length=body.max_length,
            valid_at=body.valid_at,
        )

    @app.post("/v1/feedback")
    def feedback(body: FeedbackRequest):
        return service.feedback(body)

    @app.get("/v1/sync/batch")
    def sync_batch(limit: Annotated[int, Query(ge=1, le=500)] = 500):
        batch = service.next_sync_batch(limit=limit)
        if batch is None:
            return JSONResponse(status_code=204, content=None)
        return batch

    @app.post("/v1/sync/ack")
    def sync_ack(body: SyncAckRequest):
        try:
            return {"acknowledged": service.acknowledge_sync(body.last_sequence)}
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.websocket("/ws")
    async def websocket_updates(websocket: WebSocket):
        await websocket.accept()
        origin = websocket.headers.get("origin")
        if (
            origin is not None
            and "*" not in config.allowed_origins
            and origin not in config.allowed_origins
        ):
            await websocket.close(code=4403, reason="origin not allowed")
            return
        if config.token is not None:
            header_authorized = _authorized(websocket.headers.get("authorization"), config.token)
            if not header_authorized:
                try:
                    auth_message = await asyncio.wait_for(websocket.receive_json(), timeout=5)
                except Exception:
                    await websocket.close(code=4401, reason="authentication required")
                    return
                supplied = auth_message.get("token") if isinstance(auth_message, dict) else None
                if (
                    not isinstance(auth_message, dict)
                    or auth_message.get("type") != "brainhub.auth"
                    or not isinstance(supplied, str)
                    or not hmac.compare_digest(supplied, config.token)
                ):
                    await websocket.close(code=4401, reason="invalid bearer token")
                    return
        queue = service.subscribe()
        try:
            last_version = service.store.projection_version()
            await websocket.send_json(
                {
                    "type": "projection.ready",
                    "projection_version": last_version,
                }
            )
            while True:
                try:
                    message = await asyncio.wait_for(
                        queue.get(),
                        timeout=max(0.05, config.websocket_poll_interval_seconds),
                    )
                except TimeoutError:
                    current_version = service.store.projection_version()
                    if current_version == last_version:
                        continue
                    message = {
                        "type": "projection.updated",
                        "projection_version": current_version,
                        "external_process": True,
                    }
                await websocket.send_json(message)
                message_version = message.get("projection_version")
                if isinstance(message_version, int):
                    last_version = message_version
        except WebSocketDisconnect:
            pass
        finally:
            service.unsubscribe(queue)

    return app
