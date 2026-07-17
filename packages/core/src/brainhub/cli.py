"""Brain Hub administration and local daemon CLI."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Annotated

import typer

from .api import ApiSettings, create_app
from .crypto import ContentCipher, DefaultKeyProvider
from .demo import demo_event, seed_demo
from .mcp_server import run_mcp
from .models import BrainEvent, FeedbackRequest, stable_id
from .service import BrainHubService
from .store import DemoResetRefused, EventStore


app = typer.Typer(
    name="brainhub",
    no_args_is_help=True,
    help="Local-first evidence-backed graph memory for AI agent workstreams.",
)


def default_db_path() -> Path:
    configured = os.environ.get("BRAINHUB_DB_PATH") or os.environ.get("BRAINHUB_DB")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".local" / "share" / "brainhub" / "brainhub.db"


def build_service(
    db_path: Path | None = None,
    *,
    semantic: bool | None = None,
) -> BrainHubService:
    selected = (db_path or default_db_path()).expanduser()
    key_account = stable_id("installation", str(selected.resolve()))
    cipher = ContentCipher(DefaultKeyProvider(key_account))
    store = EventStore(selected, cipher)
    enabled = (
        os.environ.get("BRAINHUB_SEMANTIC", "true").casefold() not in {"0", "false", "no"}
        if semantic is None
        else semantic
    )
    return BrainHubService(store, enable_semantic=enabled)


def _write(value) -> None:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    typer.echo(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


@app.command()
def serve(
    db: Annotated[Path | None, typer.Option(help="SQLite database path.")] = None,
    host: Annotated[str, typer.Option(help="Bind address.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(min=1, max=65535)] = 8420,
    allow_non_loopback: Annotated[
        bool, typer.Option(help="Acknowledge exposure beyond the local machine.")
    ] = False,
    no_semantic: Annotated[bool, typer.Option(help="Use explicit lexical degraded mode.")] = False,
) -> None:
    """Run the REST/WebSocket daemon on 127.0.0.1:8420 by default."""

    non_loopback = host not in {"127.0.0.1", "localhost", "::1"}
    if non_loopback and not allow_non_loopback:
        raise typer.BadParameter("non-loopback bind requires --allow-non-loopback")
    token = os.environ.get("BRAINHUB_API_TOKEN")
    if non_loopback and (token is None or not token.strip()):
        raise typer.BadParameter(
            "non-loopback bind requires a nonempty BRAINHUB_API_TOKEN"
        )
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise typer.BadParameter("uvicorn is not installed") from exc
    service = build_service(db, semantic=not no_semantic)
    # Uvicorn access logs include full URLs; keeping them off by default prevents
    # GET query text and opaque node identifiers from leaking into terminal logs.
    uvicorn.run(
        create_app(service, settings=ApiSettings(token=token)),
        host=host,
        port=port,
        access_log=False,
    )


@app.command("mcp")
def mcp_command(
    db: Annotated[Path | None, typer.Option(help="SQLite database path.")] = None,
) -> None:
    """Run the local MCP server over stdio (keeps stdout protocol-clean)."""

    service = build_service(db)
    try:
        run_mcp(service)
    finally:
        service.close()


@app.command("record")
def record_command(
    input_path: Annotated[
        str, typer.Argument(help="CloudEvents JSON file, or '-' for stdin.")
    ],
    db: Annotated[Path | None, typer.Option(help="SQLite database path.")] = None,
) -> None:
    raw = sys.stdin.read() if input_path == "-" else Path(input_path).read_text(encoding="utf-8")
    event = BrainEvent.model_validate_json(raw)
    service = build_service(db)
    try:
        _write(service.record(event))
    finally:
        service.close()


@app.command("search")
def search_command(
    query: Annotated[str, typer.Argument(min=1)],
    db: Annotated[Path | None, typer.Option(help="SQLite database path.")] = None,
    anchor: Annotated[str | None, typer.Option(help="Strict neighborhood anchor.")] = None,
    hops: Annotated[int, typer.Option(min=0, max=2)] = 2,
    limit: Annotated[int, typer.Option(min=1, max=100)] = 20,
    global_scope: Annotated[
        bool, typer.Option("--global", help="Explicitly search the entire graph.")
    ] = False,
) -> None:
    if anchor is None and not global_scope:
        raise typer.BadParameter("pass --anchor for scoped search or --global explicitly")
    service = build_service(db)
    try:
        _write(
            service.search(
                query,
                anchor_id=anchor,
                hops=hops,
                limit=limit,
                global_scope=global_scope,
            )
        )
    finally:
        service.close()


@app.command("get-node")
def get_node_command(
    node_id: str,
    db: Annotated[Path | None, typer.Option(help="SQLite database path.")] = None,
) -> None:
    service = build_service(db)
    try:
        node = service.get_node(node_id)
        if node is None:
            raise typer.BadParameter(f"node not found: {node_id}")
        _write(node)
    finally:
        service.close()


@app.command("expand")
def expand_command(
    node_id: str,
    db: Annotated[Path | None, typer.Option(help="SQLite database path.")] = None,
    hops: Annotated[int, typer.Option(min=0, max=2)] = 1,
) -> None:
    service = build_service(db)
    try:
        _write(service.expand(node_id, hops=hops))
    finally:
        service.close()


@app.command("path")
def path_command(
    source_id: str,
    target_id: str,
    db: Annotated[Path | None, typer.Option(help="SQLite database path.")] = None,
    directed: bool = False,
    max_length: Annotated[int, typer.Option(min=1, max=12)] = 8,
) -> None:
    service = build_service(db)
    try:
        _write(
            service.path(
                source_id, target_id, directed=directed, max_length=max_length
            )
        )
    finally:
        service.close()


@app.command("feedback")
def feedback_command(
    target_id: str,
    verdict: Annotated[
        str, typer.Option(help="accept, reject, needs_review, incorrect, or duplicate")
    ],
    note: Annotated[str | None, typer.Option(max=2000)] = None,
    db: Annotated[Path | None, typer.Option(help="SQLite database path.")] = None,
) -> None:
    service = build_service(db)
    try:
        _write(service.feedback(FeedbackRequest(target_id=target_id, verdict=verdict, note=note)))
    finally:
        service.close()


@app.command("import-graphify")
def import_graphify_command(
    graph_json: Annotated[Path, typer.Argument(exists=True, readable=True)],
    db: Annotated[Path | None, typer.Option(help="SQLite database path.")] = None,
    workspace_id: Annotated[str, typer.Option(max=256)] = "graphify-import",
) -> None:
    service = build_service(db)
    try:
        _write(service.import_graphify(graph_json, workspace_id=workspace_id))
    finally:
        service.close()


@app.command("demo")
def demo_command(
    db: Annotated[Path | None, typer.Option(help="SQLite database path.")] = None,
    reset: Annotated[
        bool,
        typer.Option(
            "--reset",
            help="Reset only an empty/demo-only DB; refuses databases with personal events.",
        ),
    ] = False,
) -> None:
    """Seed the same deterministic graph shown by the offline web console."""

    service = build_service(db)
    try:
        if reset:
            try:
                service.store.reset_if_only_events({demo_event().id})
            except DemoResetRefused as exc:
                raise typer.BadParameter(str(exc)) from exc
        _write(seed_demo(service))
    finally:
        service.close()


@app.command("sync-batch")
def sync_batch_command(
    db: Annotated[Path | None, typer.Option(help="SQLite database path.")] = None,
    limit: Annotated[int, typer.Option(min=1, max=500)] = 500,
) -> None:
    service = build_service(db, semantic=False)
    try:
        batch = service.next_sync_batch(limit=limit)
        _write(batch if batch is not None else {"events": []})
    finally:
        service.close()


if __name__ == "__main__":  # pragma: no cover
    app()
