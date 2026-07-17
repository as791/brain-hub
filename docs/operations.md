# Operations and performance

## Initial service objectives

The local v1 target is 100,000 nodes and 1,000,000 edges/events on a developer laptop.

- warm search p95 below 250 ms for a bounded result set;
- a captured event searchable within 30 seconds;
- hook execution p95 below 50 ms, excluding asynchronous delivery;
- common two-hop expansion below 150 ms;
- browser scene budget of 2,000 nodes and 10,000 edges;
- no loss under process restart with at-least-once spool replay.

These are acceptance targets, not benchmark claims. The release pipeline must publish hardware, dataset, percentile method, cold/warm state, and degraded-search status with every result.

## Health signals

Expose readiness separately from liveness. Readiness requires the event store, projection checkpoint, and query service; Semble can be degraded without making writes unavailable. Report event lag, projection lag, spool depth/age, search snapshot version, WebSocket clients, redaction counters, duplicate/conflict counts, and sync cursor/error state.

Metrics and logs must never include captured content, auth headers, keys, or raw query text by default.

## Backup and recovery

Pause the projector at a checkpoint, use SQLite's online backup API, encrypt the result, and record the highest event sequence. Recovery restores events first, discards derived tables/indexes, replays the pinned projector, and verifies counts/hashes. Cloud graph state is a rebuildable replica, not a backup of local content.

## Upgrade discipline

Event formats are versioned and immutable. Database migrations are forward-only and transactional. Keep a supported downgrade path by preserving the prior binary and database backup, not by attempting reverse interpretation of new events. Adapter capability probes are pinned and unknown host fields are tolerated.
