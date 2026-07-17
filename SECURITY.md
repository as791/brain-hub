# Security policy

Do not report vulnerabilities through graph content, public issues, or agent prompts. Before public release, replace this development policy with a monitored private security contact owned by the publisher.

The daemon binds to loopback by default. Treat any configuration that listens on another interface as a deployment: set a strong API token, terminate TLS at a trusted proxy, constrain CORS origins, isolate the database, and review the threat model in `docs/security.md`.

Never commit `.env`, master keys, local databases, spools, transcripts, or captured artifacts. Rotate credentials and delete/rebuild derived indexes if any of those materials are exposed.
