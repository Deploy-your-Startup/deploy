# Deployment scheduling

Service verification is cancellable per caller workflow, ref, environment and
application. Manual verification runs independently. Publication starts only
after successful verification or validated equivalent PR evidence; the existing
integration, lint, coverage and enabled browser gates remain in place.

## One deployment lock per environment

Service publication, direct deployment and infrastructure deployment share
`startup-deploy-<environment>` in the consumer repository. The non-cancelling
lock spans image publication, the complete service matrix and final health.
It deliberately excludes workflow names, service names and refs. Running
deployments finish; GitHub may replace a pending job with a newer arrival, so
the lock does not promise FIFO delivery or that every manual request runs.

Infrastructure readiness is awaited before acquiring this lock. The application
paths recheck readiness with a zero timeout inside it. If newer infrastructure
has become queued, active or failed, application publication fails immediately
and must be rerun after provisioning succeeds. It never waits for another job
that needs the same lock. BYOS skips managed-infrastructure history as before.

Inside the lock, build and direct-deploy pushes check whether a newer push to
the same caller workflow and branch exists. Older automatic runs skip mutation,
even if newer checks failed. API errors fail closed. Documentation pushes that
do not trigger that caller workflow do not suppress a valid deployment. Manual
redeployment can deliberately select a historical revision. A push arriving
after authorization cannot interrupt the current deployment.

Cloudflare Pages uses a separate non-cancelling lock per Pages project across
refs. Its callers still control PR exclusion and optional browser dependencies.

## Configuration and health

After installing project dependencies and roles, the shared deploy action runs
`startup ansible validate` (CLI v0.1.14 or later) on the real project playbook with the action's static
inventory. This performs no provider inventory lookup or server mutation. A
syntax error stops before joining the tailnet or opening SSH. It does not prove
that runtime variables or provider settings are correct.

Build, direct-deploy and infrastructure callers accept optional `health_url`:

```yaml
with:
  environment: production
  health_url: https://app.example.com/health
```

The hosted runner requires a trusted HTTPS connection and HTTP 2xx within 120
seconds, after all services finish. Redirects are not accepted. URLs must not
contain credentials, query strings or fragments. Output includes neither URLs
nor response bodies. Failed health fails the deployment job without automatic
rollback. Empty configuration explicitly records skipped, unverified health;
leave it empty for first infrastructure provisioning and inaccessible private
sites. A healthy endpoint does not establish complete E2E correctness.

## Limits and adoption

CLI operations and deployments from separate repositories sharing one cluster
are outside GitHub's repository-scoped lock. Coordinate those operations.
Caller-level cancellation and explicit human cancellation can still interrupt
jobs. A failed service can leave a partially updated application; there is no
transactional multi-service rollback.

Before updating to v2, rename any `build-and-deploy-service-parallel.yml` caller
to `build-and-deploy-service.yml`. Old release pins remain available. Review
branch rules that name CI workflows or `verify / Required checks` after renaming.
Advance application pins separately; merging or releasing changes no project.

Local tests execute the freshness/readiness commands, TLS endpoint checks and
gate scripts, and check the shipped scheduler definitions. They do not execute
GitHub's scheduler. Qualify a disposable consumer before production adoption:
overlapping infrastructure/direct/build deployments must serialize; queued
infrastructure must not deadlock; obsolete pushes must skip; failed browser
checks must block every service; and health failure must remain a failed run.

Compare commit-to-deployment elapsed time, summed runner job minutes, artifact
storage and actual billing before changing PR reuse or shard counts. Include
direct pushes, successful reuse and fallback. Existing PR reuse is unchanged;
no savings are claimed without representative measurements.
