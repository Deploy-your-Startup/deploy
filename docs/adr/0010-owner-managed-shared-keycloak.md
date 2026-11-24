# 0010: Owner-managed shared Keycloak

## Context

Startups sharing a trusted cluster need reusable authentication without tying
identity data to an application's storage or lifetime. Existing OIDC consumers
must retain their configuration when these optional roles become available.

## Decision

The cluster owner's project manages one Keycloak installation from its existing
playbook and group_vars. Authentication has its own namespace, PostgreSQL volume
and generated recovery Secret. Attached projects may reuse the installation but
cannot create or upgrade it. Normal deployments preserve an existing server;
an explicit owner deployment with `keycloak_upgrade: true` applies reviewed
updates. Reuse still checks workload, recovery Secret and ingress certificate readiness. Removing an application never uninstalls shared authentication.

The cluster owner declares each startup's realm, confidential PKCE S256 client,
and target namespace in versioned configuration. The owner alone runs the
client role with the shared administrator credential. The startup's deployment
consumes its own client Secret. Removing a declaration leaves resources in
place for explicit retirement. The client role retains users, registration
policy and persisted credentials.
Only explicitly declared session and passkey settings are reconciled. Sharing a
realm is an operator decision, with one owner for its declared realm policy.
OAuth2 Proxy accepts the generated namespaced client Secret and an explicit
issuer/provider; its existing Auth0 defaults remain unchanged.

Ingress TLS and a trusted external TLS proxy are supported. The latter normalizes
the fixed public HTTPS origin only on the private ingress path. Both require
production DNS, certificates and routing to be verified independently.

The cluster owner backs up the authentication database and recovery credentials
separately. Losing a Secret for existing storage requires recovery, not automatic
credential replacement. Keep the existing namespace and hostname during adoption;
changing a namespace does not migrate data or users. Before retiring an owner
project, transfer its operator settings and access to a successor. Cluster
removal requires an explicit decision to migrate, stop or delete identity data.

## Consequences

Authentication roles are opt-in and distributed through the normal deployment
release and project pin. No new CLI command or automatic application-template
migration is required. Applications retain responsibility for authorization and
for migrating any existing login system.

Local integration checks exercise the real roles against a Kubernetes boundary.
Runtime and browser scenarios use real Keycloak, PostgreSQL and OAuth2 Proxy,
including backup/restore and passkey enrollment. They do not establish production
DNS, certificate issuance, disaster recovery or migration safety. The shared
single-replica database uses local storage and has no automatic host failover.
