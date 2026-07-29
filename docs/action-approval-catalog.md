# Action and approval catalog

Brain Hub's portal is evidence-first. A displayed task, blocker, test result, or suggested
action is information, not authority to act.

| Action class | Current status | Required authority and evidence |
| --- | --- | --- |
| Read local graph, pending tasks, blockers, and test results | Available | Read-only; show node evidence and confidence. |
| Start a local agent in `ask` mode | Available on loopback | Explicit user initiation; read-only sandbox; persisted local job status. |
| Start a local agent in `work` mode | Available on loopback | Explicit user initiation; workspace-write warning; persisted local job status and exit code. |
| Stage, commit, push, or open a pull request | Not a portal action | Separate, explicit repository authority; record target, actor, result, and immutable evidence. |
| Publish or deploy a site or package | Not implemented | Separate, explicit approval for the exact target and version; preflight evidence and resulting deployment identity. |
| Change cloud/server resources or access credentials | Not implemented | Authenticated external authority, least-privilege scope, explicit approval, audit identity, and redacted result evidence. |
| Destructive or irreversible maintenance | Intentionally absent | Local authenticated admin workflow plus target confirmation and recovery plan; never agent-facing by default. |

Before any external action is added, its contract must define the exact target, dry-run or
preview behavior, approval lifetime, actor identity, idempotency key, result states,
redaction policy, evidence locator, retry rules, and a safe failure/rollback path. Pending
or blocked graph records must never be interpreted as implicit approval.
