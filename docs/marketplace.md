# Marketplace launch plan

The repository plugin is installable locally. Public marketplace submission remains a release process, not a value that should be faked in source.

## Public release gates

- Host the MCP server on an HTTPS Streamable HTTP endpoint with health monitoring and stable tool schemas.
- Implement OAuth with correct audience binding and no token passthrough.
- Verify the publisher identity and domain; publish real privacy policy, terms, support, and security-contact URLs.
- Replace development author/contact metadata with the legal publisher identity.
- Complete license review for the AGPL/Apache/commercial split and all transitive UI dependencies.
- Produce signed release artifacts, checksums, an SBOM, provenance, and a rollback procedure.
- Run abuse, privacy, deletion, load, accessibility, and cross-platform installation tests.
- Capture approved screenshots and final icon assets from the production build.

## Submission acceptance cases

Five positive cases:

1. Record a new work goal and retrieve its workstream, run, topic, and evidence.
2. Continue a workstream in a different agent and preserve cross-agent provenance.
3. Search from a selected node with a strict two-hop boundary and no global leakage.
4. Explain a relationship path with direction, confidence, evidence, and time.
5. Correct an assertion while preserving the superseded fact and its recorded-time history.

Three negative cases:

1. Refuse or redact transcript content when raw capture is disabled.
2. Reject or redact secrets instead of storing/indexing them.
3. Refuse destructive deletion through an MCP tool; require the authenticated admin UI/CLI path.

Each case must be recorded as a deterministic fixture and must pass against the hosted endpoint, not only mocks.

## Product positioning

The defensible value is trustworthy cross-agent continuity: passive but consent-aware capture, evidence-backed temporal graph memory, corrections that preserve history, anchored retrieval, and a visualization that remains useful as the graph grows. A flashy graph without these properties is not a marketplace-ready memory system.
