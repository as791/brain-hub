# Brain Hub

Brain Hub is a local-first, evidence-backed memory graph for work performed across Codex, Claude Code, Cursor, Antigravity, and other agent hosts. It captures durable workstream events, projects semantic topics and relationships into a typed temporal multigraph, and exposes the graph through CLI, REST, WebSocket, and MCP interfaces.

The product treats agent memory as auditable data rather than a transcript dump:

- one logical idea, decision, task, claim, artifact, run, actor, workspace, or workstream per node;
- typed, directed edges with plain-language explanations, confidence, evidence, and valid/recorded time;
- immutable source events and replayable projections so extraction can improve without rewriting history;
- exact-identity merges only; similar ideas remain distinct until a user confirms a duplicate through review feedback;
- local encrypted content, metadata/summary capture by default, and graph-only cloud sync when explicitly enabled;
- Semble hybrid retrieval plus bounded graph traversal, with a two-hop default and an explicit maximum depth of 20 anchored at a selected node;
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

## Install

The installer needs Python 3.11 or newer; end users do not need Node.js, npm,
Docker, or an activated virtual environment.

These `main`-channel commands install the latest public preview. They intentionally
follow a mutable branch and are not reproducible release installs.

macOS or Linux:

```sh
sh -c 'set -eu; f=$(mktemp); cleanup() { rm -f "$f"; }; trap cleanup EXIT; curl --proto "=https" --tlsv1.2 --max-filesize 1048576 -fsSLo "$f" https://raw.githubusercontent.com/as791/brain-hub/main/scripts/install.sh; sh "$f"' && export PATH="$HOME/.local/bin:$PATH"
```

Windows PowerShell:

```powershell
[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; $p=Join-Path ([IO.Path]::GetTempPath()) ("brainhub-"+[Guid]::NewGuid().ToString("N")+".ps1"); try { Invoke-WebRequest https://raw.githubusercontent.com/as791/brain-hub/main/scripts/install.ps1 -MaximumRedirection 0 -OutFile $p; if((Get-Item $p).Length -gt 1MB){throw "Installer is larger than 1 MB"}; powershell.exe -NoProfile -ExecutionPolicy Bypass -File $p; if ($LASTEXITCODE) { exit $LASTEXITCODE } } finally { Remove-Item $p -Force -ErrorAction SilentlyContinue }; $env:Path="$HOME\.local\bin;$env:Path"
```

### Pin the preview source

`BRAINHUB_REF` selects the source fetched after an installer wrapper starts. Setting it
while downloading the wrapper itself from `main` does not pin that outer wrapper. To
source-pin a preview, choose a reviewed full commit SHA and use it for both the wrapper
URL and `BRAINHUB_REF`.

macOS or Linux:

```sh
(
  REF=0123456789abcdef0123456789abcdef01234567 # replace with the reviewed commit
  if [ "${#REF}" -ne 40 ]; then echo "REF must be a full commit SHA" >&2; exit 2; fi
  case "$REF" in *[!0-9A-Fa-f]*) echo "REF must be a full commit SHA" >&2; exit 2;; esac
  BRAINHUB_REF="$REF" sh -c 'set -eu; f=$(mktemp); cleanup() { rm -f "$f"; }; trap cleanup EXIT; curl --proto "=https" --tlsv1.2 --max-filesize 1048576 -fsSLo "$f" "https://raw.githubusercontent.com/as791/brain-hub/${BRAINHUB_REF}/scripts/install.sh"; BRAINHUB_REF="$BRAINHUB_REF" sh "$f"'
) && export PATH="$HOME/.local/bin:$PATH"
```

Windows PowerShell:

```powershell
$Ref = "0123456789abcdef0123456789abcdef01234567" # replace with the reviewed commit
if ($Ref -notmatch "^[0-9A-Fa-f]{40}$") { throw "Ref must be a full commit SHA" }
$env:BRAINHUB_REF = $Ref
$p = Join-Path ([IO.Path]::GetTempPath()) ("brainhub-"+[Guid]::NewGuid().ToString("N")+".ps1")
try {
    Invoke-WebRequest "https://raw.githubusercontent.com/as791/brain-hub/$Ref/scripts/install.ps1" -MaximumRedirection 0 -OutFile $p
    if ((Get-Item $p).Length -gt 1MB) { throw "Installer is larger than 1 MB" }
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File $p
    if ($LASTEXITCODE) { exit $LASTEXITCODE }
} finally {
    Remove-Item $p -Force -ErrorAction SilentlyContinue
}
$env:Path="$HOME\.local\bin;$env:Path"
```

This pins Brain Hub's source and wrapper, but not every installed byte. The installer
resolves third-party packages from declared version ranges at installation time, so
package-index state and install time can change the selected dependency versions. The
preview runtime is source-addressed and Python-compatibility-addressed, not a
bit-for-bit reproducible artifact. Locked dependencies, signed artifacts, checksums,
an SBOM, and provenance remain marketplace publication gates.

One run installs and verifies:

- the `brainhub` CLI and bundled interactive web console;
- `brainhub-adapter` plus Codex, Claude, Cursor, and Antigravity hook commands;
- an isolated, source-addressed Python runtime that can be upgraded without moving or
  deleting personal graph data;
- the Brain Hub plugin marketplace, automatically registered with Codex when its
  CLI is available;
- the same absolute local MCP runtime registered with installed Claude Code, Cursor,
  and Antigravity hosts, without overwriting a pre-existing unrelated entry.

The installer is idempotent, adds its launcher directory to the user `PATH`, and
keeps the previous runtime available when the source changes. On interactive first
use it prefers the operating-system keychain; on non-interactive or headless first
use it creates and pins a private per-installation local key so background agents
cannot stall on an unavailable keychain prompt. Restart any open agent hosts after installation. In Codex,
review and trust the lifecycle hooks once with `/hooks`. If the command is not visible
in an already-open terminal, open a new terminal.

Verify the complete installed path:

```sh
brainhub --help
brainhub start
brainhub status
brainhub search "privacy architecture" --global
brainhub ui
```

An empty search result is normal until an agent records work. Stop the background
service with `brainhub stop`.

## Developer setup

Source contributors additionally need Node.js 20.19+ (or 22.12+):

```bash
git clone https://github.com/as791/brain-hub.git
cd brain-hub
python3 -m venv .venv
source .venv/bin/activate
python -m pip install '.[dev]' ./adapters
cp .env.example .env
cd apps/web && npm install && npm run build && cd ../..
```

Continuously drain passive hook events without adding network latency to the agent:

```bash
cd brain-hub
source .venv/bin/activate
set -a
source .env
set +a
brainhub-adapter watch
```

The empty `BRAINHUB_DB_PATH` uses Brain Hub's per-user data directory so the HTTP daemon and the plugin's stdio MCP process share one SQLite authority. If you override it, use the same absolute path in every process.

The stdio MCP process opens the shared SQLite authority directly and drains passive
hook events without requiring network ports. Installed users can start and open the
optional local API and console directly:

```bash
brainhub ui
```

The command starts the managed background service if needed, verifies both API and UI
identities, opens `http://127.0.0.1:4173`, and exits. Use `brainhub start`,
`brainhub status`, and `brainhub stop` for explicit lifecycle control. Frontend
contributors can still run `npm install` and `npm run dev` from `apps/web`. The UI can
run against its bundled demonstration graph when the daemon is unavailable; a status
badge makes that state explicit.

The main quick start intentionally opens an empty personal graph. To explore seeded data without mixing fake records into that graph, use a disposable database in two terminals:

```bash
BRAINHUB_DB_PATH=/tmp/brainhub-demo.db brainhub demo --reset
BRAINHUB_DB_PATH=/tmp/brainhub-demo.db brainhub serve --host 127.0.0.1 --port 8420
```

For an isolated local deployment, set a random 32-byte URL-safe base64 master key and a strong API token, then run `docker compose up --build`. The API binds to `127.0.0.1:8420` and the web console to `127.0.0.1:4173`; no cloud service is required.

## Agent and plugin setup

The main installer already copies and registers the Codex plugin. Its generated MCP
configuration points directly to the verified managed runtime, so Codex Desktop does
not depend on an activated project shell or the terminal `PATH`. Its trusted Codex
lifecycle hooks capture bounded metadata automatically, while either the plugin MCP
process or the optional supervised service drains the spool. Review and trust those
commands once in Codex with `/hooks`.

The plugin also provides an explicit skill for recording, searching, expanding, and
correcting graph memory. The installer registers its MCP tools with installed Claude
Code, Cursor, and Antigravity hosts. Their optional passive lifecycle hooks can use the
matching installed hook command described in the [adapter guide](adapters/README.md).
A hook failure never blocks an agent session.

## Default privacy boundary

Brain Hub stores raw source events locally. Content fields are encrypted before SQLite writes. Search indexing receives a redacted, temporary projection and is not allowed to persist its own copy. Cloud sync is disabled by default and, when enabled, sends only selected graph facts and opaque evidence citations—not prompts, transcripts, tool arguments, secrets, absolute paths, or hidden reasoning.

See [architecture](docs/architecture.md), [data contract](docs/data-contract.md), [threat model](docs/security.md), and [marketplace launch gates](docs/marketplace.md) before exposing the daemon beyond loopback or publishing the plugin.

## Scope

This implementation is the runnable local product plus a hardened, tested cloud sync schema. The hosted control plane and AGE tenant-query projection remain publication gates, not deployment-ready claims. Team administration, raw cloud content, mobile/VR clients, A2A delegation, transcript database scraping, and a literal fourth spatial dimension are intentionally outside v1.

## Licensing model

The intended open-core split is AGPL-3.0-or-later for the daemon and UI, Apache-2.0 for schemas, SDK contracts, and adapters, and a separate commercial license for the hosted control plane. The source headers and release packaging must be finalized with the publisher's legal identity before public distribution; see [licensing](docs/licensing.md).
