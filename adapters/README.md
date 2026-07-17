# Brain Hub adapters

These adapters turn stable agent integration surfaces into the shared Brain Hub
CloudEvents contract. Hook execution only writes a bounded local spool; it never waits
for the daemon or a network service. A separate foreground worker or explicit `flush`
command delivers the spool later with the event ID as an idempotency key.

## Supported boundaries

| Agent | Accepted stable surfaces | Hook command |
| --- | --- | --- |
| Codex | hooks, opt-in OTLP, `exec --json`, MCP | `brainhub-codex-hook` |
| Claude Code | hooks, opt-in OTLP, stream JSON, MCP | `brainhub-claude-hook` |
| Cursor | hooks, documented JSON/SDK streams, MCP | `brainhub-cursor-hook` |
| Antigravity | hooks, documented SDK streams, MCP | `brainhub-antigravity-hook` |

Wire the agent's documented JSON payload to the matching command on standard input.
Version-specific configuration stays outside Brain Hub so an upstream settings change
does not change the event contract. For a custom plugin, publish a manifest conforming
to `manifests/adapter.schema.json` and invoke:

```sh
brainhub-adapter capture --agent generic --mode hook
```

OTLP event attributes and structured-stream records use the same normalizer:

```sh
brainhub-adapter capture --agent codex --mode otlp
brainhub-adapter capture --agent claude --mode stream
brainhub-adapter flush --endpoint http://127.0.0.1:8420/v1/events
```

## Supervise continuous delivery

Run exactly one foreground worker for each configured spool and supervise it with the
operating system's service manager (for example, launchd, systemd, or a container restart
policy):

```sh
export BRAINHUB_API_TOKEN='the-same-token-as-the-local-daemon'
brainhub-adapter watch --endpoint http://127.0.0.1:8420/v1/events
```

The worker drains queued events without changing hook latency. When the daemon is down it
uses bounded exponential backoff with jitter; successful delivery resets the backoff.
`SIGINT` and `SIGTERM` stop it cleanly. `BRAINHUB_ENDPOINT` changes the default endpoint,
and `--token-env NAME` reads the token from an alternative environment variable without
putting the secret in the process command line. Adapter manifests expose this foreground
delivery command so installers can create the appropriate supervised service.

Permanent record failures (`400`, `409`, `413`, and `422`) move atomically into the bounded
`quarantine/` directory so one bad record cannot block later work. Authentication failures,
rate limits, and server errors stay queued and retry. Flush results and worker audit logs
report the quarantine path and count. Configure bounds with
`BRAINHUB_QUARANTINE_MAX_EVENTS` and `BRAINHUB_QUARANTINE_MAX_BYTES`, or relocate it with
`BRAINHUB_QUARANTINE`.

## Privacy contract

- Prompts, messages, tool input/output, credentials, cookies, and transcript data are
  never copied from source payloads.
- `transcript_path` and agent-owned databases are never opened. Their formats are not
  public ingestion APIs.
- Absolute workspace and artifact paths become deterministic opaque references before
  an event reaches disk.
- Metadata is the default. A summary is included only when the source explicitly
  supplies `brainhub_summary`, `summary`, or `semantic_summary`.
- The spool defaults to 1,000 events or 10 MiB. It prunes oldest events and deduplicates
  retries by deterministic event ID.

Spool and quarantine files contain only the adapter's redacted metadata and explicitly
supplied semantic summaries. Their directories are forced to mode `0700` and records to
`0600`, but these short-lived queues are not encrypted. Use an encrypted home volume and
keep both directories out of backups, source control, and shared folders.

`BRAINHUB_SPOOL`, `BRAINHUB_SPOOL_MAX_EVENTS`, and `BRAINHUB_SPOOL_MAX_BYTES` configure
the local queue. `BRAINHUB_API_TOKEN` authenticates `brainhub-adapter flush` to a
token-protected daemon. Capture entrypoints intentionally return success even for malformed or
oversized input so Brain Hub cannot interrupt the agent that is doing the work.

## Developing

From this directory:

```sh
python -m unittest discover -s tests -v
```
