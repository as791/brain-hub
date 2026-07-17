# Brain Hub web console

The console renders a bounded projection of Brain Hub as either a WebGL 3D graph, a Canvas 2D graph, or a keyboard-accessible list. “4D” means three spatial dimensions plus the explicit valid-time control at the bottom of the scene.

## Run locally

Requires Node 20.19+ (or 22.12+).

```bash
npm install
npm run dev
```

The client tries `http://127.0.0.1:8420` and falls back to a representative demo graph. Configure it with:

| Variable | Default | Purpose |
| --- | --- | --- |
| `VITE_BRAINHUB_API_URL` | `http://127.0.0.1:8420` | REST daemon base URL |
| `VITE_BRAINHUB_WS_URL` | REST URL with `ws` scheme | WebSocket base URL |
| `VITE_BRAINHUB_DEMO` | `fallback` | `force`, `fallback`, or `off` |
| `VITE_BRAINHUB_API_TOKEN` | unset | Development-only Bearer token |

All routes live in `src/api.ts`. Canonical daemon JSON is snake_case; the compatibility layer normalizes both snake_case and camelCase recursively into the typed UI model.

For runtime authentication, use the connection settings in the header. The token is held only in `sessionStorage`, applied as a REST Bearer token, never echoed by the UI, and removed when the tab closes. Because browser WebSockets cannot set an Authorization header, the client sends a `brainhub.auth` first frame; a hosted deployment should prefer a same-origin HttpOnly cookie, while an explicitly loopback-only daemon may exempt `/ws`.

## Interaction contract

- Search is always anchored and has a strict one-to-three-hop boundary. There is no silent global fallback.
- Clicking a node opens evidence, provenance, confidence, valid time, and visible relationships. “Start searches here” makes it the next anchor.
- “Explain path” highlights the evidence path from the current anchor and shows its confidence floor.
- The renderer caps a scene at 2,000 nodes and 10,000 edges. The daemon should cluster or page larger projections.
- WebGL failure switches to 2D. The List view is available at all times and supports keyboard navigation.
- `prefers-reduced-motion` disables camera and force-layout transitions.

## Verify

```bash
npm test
npm run build
npm audit --audit-level=high
```
