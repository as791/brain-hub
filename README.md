# Brain Hub

Brain Hub is a local-first, evidence-backed memory graph for work performed across Codex, Claude Code, Cursor, Antigravity, and other agent hosts. It captures durable workstream events, projects semantic topics and relationships into a typed temporal multigraph, and exposes the graph through CLI, REST, WebSocket, and MCP interfaces.

The product treats agent memory as auditable data rather than a transcript dump:

- one logical idea, decision, task, claim, artifact, run, actor, workspace, or workstream per node;
- typed, directed edges with plain-language explanations, confidence, evidence, and valid/recorded time;
- immutable source events and replayable projections so extraction can improve without rewriting history;
- exact-identity merges only; similar ideas remain distinct until a user confirms a duplicate through review feedback;
- local encrypted content, metadata/summary capture by default, and graph-only cloud sync when explicitly enabled;
- Semble hybrid retrieval plus bounded graph traversal, including hard two-hop searches anchored at a selected node;
- a WebGL 3D graph with a time dimension, camera focus, evidence drawer, and accessible 2D/list fallbacks.

## Repository map

```text
packages/core/       Python daemon, event store, graph, search, REST, MCP, and CLI
apps/web/            React/Three interactive temporal graph
adapters/            Stable capture adapters and generic adapter contract
plugins/brain-hub/   Installable Codex plugin
cloud/               Optional Postgres/Apache AGE graph-sync schema
docs/                Architecture, contracts, security, operations, and launch gates
```

## Quick start

Prerequisites are Python 3.11+ and Node.js 20+.

```bash
cd brain-hub
python3 -m venv .venv
source .venv/bin/activate
python -m pip install '.[dev]' ./adapters
cp .env.example .env
set -a
source .env
set +a
brainhub serve --host 127.0.0.1 --port 8420
```

The sourced example enables bearer authentication. Open the console's connection settings and enter `local-development-only`, or replace it with your own token. In another supervised terminal, continuously drain passive hook events without adding network latency to the agent:

```bash
cd brain-hub
source .venv/bin/activate
set -a
source .env
set +a
brainhub-adapter watch
```

The empty `BRAINHUB_DB_PATH` uses Brain Hub's per-user data directory so the HTTP daemon and the plugin's stdio MCP process share one SQLite authority. If you override it, use the same absolute path in every process.

In another terminal:

```bash
cd brain-hub/apps/web
npm install
npm run dev
```

Open the URL printed by Vite. The UI can run against its bundled demonstration graph when the daemon is unavailable; a status badge makes that state explicit.

The main quick start intentionally opens an empty personal graph. To explore seeded data without mixing fake records into that graph, use a disposable database in two terminals:

```bash
BRAINHUB_DB_PATH=/tmp/brainhub-demo.db brainhub demo --reset
BRAINHUB_DB_PATH=/tmp/brainhub-demo.db brainhub serve --host 127.0.0.1 --port 8420
```

For an isolated local deployment, set a random 32-byte URL-safe base64 master key and a strong API token, then run `docker compose up --build`. The API binds to `127.0.0.1:8420` and the web console to `127.0.0.1:4173`; no cloud service is required.

## Agent and plugin setup

Install the repository marketplace, then enable the `brain-hub` plugin in Codex:

```bash
make install-plugin-runtime
codex plugin marketplace add "$PWD"
codex plugin add brain-hub@brain-hub
```

The managed runtime lives at `~/.local/share/brainhub/venv`, so Codex Desktop can start the local MCP process without inheriting an activated project shell. The plugin falls back to a `brainhub` executable on `PATH` for development. It provides an explicit skill for recording, searching, expanding, and correcting graph memory. Passive capture remains opt-in and is supplied by host-specific adapters under `adapters/`; wire the matching documented host hook to its command and supervise `brainhub-adapter watch` as described in the [adapter guide](adapters/README.md). A hook failure never blocks an agent session.

## Default privacy boundary

Brain Hub stores raw source events locally. Content fields are encrypted before SQLite writes. Search indexing receives a redacted, temporary projection and is not allowed to persist its own copy. Cloud sync is disabled by default and, when enabled, sends only selected graph facts and opaque evidence citations—not prompts, transcripts, tool arguments, secrets, absolute paths, or hidden reasoning.

See [architecture](docs/architecture.md), [data contract](docs/data-contract.md), [threat model](docs/security.md), and [marketplace launch gates](docs/marketplace.md) before exposing the daemon beyond loopback or publishing the plugin.

## Scope

This implementation is the runnable local product plus a hardened, tested cloud sync schema. The hosted control plane and AGE tenant-query projection remain publication gates, not deployment-ready claims. Team administration, raw cloud content, mobile/VR clients, A2A delegation, transcript database scraping, and a literal fourth spatial dimension are intentionally outside v1.

## Licensing model

The intended open-core split is AGPL-3.0-or-later for the daemon and UI, Apache-2.0 for schemas, SDK contracts, and adapters, and a separate commercial license for the hosted control plane. The source headers and release packaging must be finalized with the publisher's legal identity before public distribution; see [licensing](docs/licensing.md).
