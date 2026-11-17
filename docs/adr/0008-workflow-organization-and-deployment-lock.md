# 0008 — Workflow organization and protected environment deployment

**Status:** Accepted; supersedes the alias retention in [0006](0006-one-build-workflow.md)
and extends the publication boundary in [0004](0004-separate-verification-and-publication.md).

## Context

The legacy parallel alias has no callers in the maintained portfolio or current
application templates. Repository documentation tests run twice. Separate direct
and infrastructure workflows can interrupt their own Ansible runs or overlap
service publication, because their concurrency groups differ.

## Decision

Remove the deprecated alias in v2, requiring callers to rename it before updating.
Keep older release tags intact. Prefix repository-owned workflows with `ci-` and
consolidate documentation tests into the existing complete role test run.

All managed Ansible deployment paths share a non-cancelling environment lock
within each consumer repository. Infrastructure readiness waits run before the
lock. A zero-timeout readiness recheck inside the lock stops if infrastructure
became pending or failed, without creating a dependency deadlock. Direct pushes
also check freshness after acquiring the lock. Pages uses a non-cancelling lock
per Cloudflare project across refs.

Validate project playbook syntax with the installed, pinned startup CLI before
server access. An explicit optional `health_url` verifies a public HTTPS endpoint
after deployment; absent configuration is reported as unverified. Keep application
E2E settings unchanged. Measure PR reuse before changing its existing mechanism.

## Consequences

GitHub can replace a pending job; this is mutual exclusion, not FIFO delivery.
Manual cancellation and caller-level cancellation remain possible. CLI runs and
different repositories attached to one cluster require operator coordination.
Multi-service deployments are not transactional. Public endpoint checks do not
cover private sites or prove complete application correctness.
