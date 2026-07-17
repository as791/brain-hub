# Optional cloud graph replica

This directory defines the graph-only sync destination. It is not required for local Brain Hub and does not accept raw prompts, transcripts, code, tool payloads, secrets, or absolute paths.

For local schema development:

```bash
docker compose -f cloud/docker-compose.yml up -d
psql postgresql://brainhub:brainhub-development-only@127.0.0.1:5438/brainhub
```

The pinned development image is Apache AGE 1.7.0 on PostgreSQL 18. Relational tables are the audited ingest/projection ledger; the `brainhub` AGE graph is a rebuildable query projection. Production must use a managed secret, TLS, backups, network isolation, per-tenant connection context, migration tooling, and a digest-pinned image.

The service must set `SET LOCAL brainhub.tenant_id = '<uuid>'` inside every transaction after validating the OAuth subject and audience. Row-level security then scopes all tenant tables. A database owner bypasses RLS and must never be used by the application.

Validated batches enter through `public.ingest_brainhub_sync_batch`. It locks the installation cursor and atomically verifies a contiguous batch, enforces idempotency by a server-computed SHA-256 of PostgreSQL's normalized `jsonb` event array, appends graph events, and advances the cursor. Each event digest covers only the policy-filtered graph payload—not the local raw event. The database recursively allowlists graph-fact fields and rejects raw-capture field names; API-level validation against `schemas/sync-batch.schema.json` remains mandatory. Grant this function only to the dedicated sync-ingest role during deployment.

The migrations create three `NOLOGIN`, `NOINHERIT` capability roles: `brainhub_sync_ingest`, `brainhub_projection_worker`, and `brainhub_graph_reader`. Grant only the required capability to a separately managed login. None receives AGE/Cypher privileges.

PostgreSQL row-level security does not automatically protect vertices and edges inside an AGE graph. Application roles must not receive direct AGE graph privileges. A trusted projection worker writes `tenant_id` on every AGE object, and tenant-scoped security-definer query functions must inject the authenticated tenant filter rather than accepting tenant Cypher from callers. Until those functions and their cross-tenant tests are installed, the relational ledger is authoritative and AGE access is deployment-blocked.
