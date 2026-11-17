# Architecture decision records

Each record states one lasting decision: context, decision and consequences.
Records 0001–0099 belong to this shared repository; projects number their own
records from 0100. Supersede a record with a new one instead of rewriting it.

| ADR | Decision |
| --- | --- |
| [0001](0001-projects-pin-releases-directly.md) | Projects pin releases of this repository directly |
| [0002](0002-reuse-verified-pr-builds.md) | Pushes may reuse a verified, equivalent PR build |
| [0003](0003-ssh-tunnel-instead-of-public-api.md) | The Kubernetes API is closed; access goes through SSH |
| [0004](0004-separate-verification-and-publication.md) | Cancel obsolete verification while protecting application publication |
| [0005](0005-local-or-ci-e2e.md) | Select local or CI browser verification without changing the test suite |
| [0006](0006-one-build-workflow.md) | One service build workflow; the parallel name is a deprecated alias |
| [0007](0007-releases-are-self-contained.md) | Each release is immutable and its references name its own tag |
| [0008](0008-workflow-organization-and-deployment-lock.md) | Remove the legacy alias and share a protected environment deployment lock |
| [0009](0009-one-project-deployment-version.md) | One native project reference configures local and CI deployment |
