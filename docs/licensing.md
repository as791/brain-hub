# Licensing plan

The intended release model is:

- daemon and web UI: `AGPL-3.0-or-later`;
- event schemas, adapter contract, SDKs, and reference adapters: `Apache-2.0`;
- hosted control plane and enterprise policy features: separately licensed.

This split prevents a hosted fork from closing improvements to the core while keeping integration contracts permissive. Before distribution, the publisher must add complete license texts, SPDX headers, copyright ownership, contributor terms, and a dependency audit. Until that release work is complete, this repository describes the intended model but should not be represented as legally finalized.
