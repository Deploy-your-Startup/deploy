# Shared Keycloak

`keycloak` deploys one production-mode Keycloak per cluster in `authentication`.
The cluster owner's project manages it from its existing `deployment/playbook.yml`
and `deployment/group_vars`, like cert-manager. Attached projects reuse the
installation. Authentication data has a separate namespace, database and retained
volume, and survives removal of an application.

Add this after `kubernetes-client` in the project's service play:

```yaml
- name: Ensure shared authentication exists
  import_role:
    name: keycloak
  when: (auth_provider | default('')) == 'keycloak'
  tags: keycloak
```

Configure the owner project's `deployment/group_vars/all.yml`:

```yaml
auth_provider: keycloak
keycloak_hostname: auth.example.com
keycloak_namespace: authentication
startup_keycloak_operator: true # permits the separate authentication backup
```

The application keeps its own `k8s_namespace` and database settings. The role
scopes its Postgres and certificate issuer to `keycloak_namespace`, including
separate volume names and storage paths. Shared-cluster attachments already set
`startup_cluster_managed: false`; they cannot install or update the server.
A legacy single-project deployment is the owner by default, as for cert-manager.

Run the existing CLI from the project:

```bash
startup ansible deploy --working-directory deployment --environment production --service keycloak
```

The first owner deployment installs the server. Later deployments leave it
unchanged, even if another project pins a different role or image version.
To apply a reviewed server configuration or version change, set
`keycloak_upgrade: true` in the owner project's group_vars for that explicit
`--service keycloak` deployment, then remove it. Verify backups and the login
flows of affected applications before updating. `keycloak_database_version` and
`keycloak_database_storage_node` configure authentication storage independently
of the application's Postgres settings; changing the node does not migrate data.
Normal realm/client deployment remains separate and can still update the
startup's declared session policy.

The role uses a dedicated PostgreSQL database, the existing retained local-volume
role, a namespace-local certificate issuer and HTTPS ingress. It reuses existing
credentials and refuses to generate replacements for an existing database whose
Secret has been lost. Keycloak runs one replica; local storage does not provide
host failover. DNS must point the authentication hostname at the cluster before
deployment. The public ingress exposes port 8080 behind TLS, never health port 9000.

The default `keycloak_tls_termination: ingress` uses the cluster's certificate
issuer. If an existing external proxy owns public HTTPS, set
`keycloak_tls_termination: external_proxy` in the independent operator config.
Configure that proxy's hostname, certificate and private HTTP upstream separately.
The role then uses the cluster's HTTP ingress, normalizes the fixed public HTTPS
origin for Keycloak, and removes its namespace Certificate to stop duplicate
issuance. It does not change other services' ingress or the global controller.
Keep that internal HTTP upstream reachable only through the trusted edge/private
network; it is not a second public login endpoint. Public discovery validation
still requires a valid HTTPS certificate. Provider-specific proxy configuration
belongs in the owner's repository, not this reusable role.

Adopt a published deployment release and review the project pin with
`startup ansible pin --working-directory deployment --version <reviewed-ref>`.
Use the project's existing encrypted access and Vault password. No separate
Keycloak command group or configuration repository is needed. See
[role distribution](../../docs/role-distribution.md): merging a change does not
update a project until it adopts a release.

The `keycloak` Secret in `authentication` contains the initial administrator
credentials and database password. Keep it in the cluster's encrypted backups.
Create a personal administrator in Keycloak for day-to-day use. The automation
administrator is used by the client role; deleting it requires deliberately
updating that automation credential too. Never print it in CI logs.

Back up authentication separately from the application using the existing CLI:

```bash
startup ansible backup --working-directory deployment --environment production \
  --playbook roles/keycloak/files/backup-playbook.yml --backup-dir /private/keycloak-backup
```

Run this from the owner project with `startup_keycloak_operator: true` and
`keycloak_namespace` configured. The shared playbook downloads a PostgreSQL
custom-format dump and its recovery Secret with private filesystem permissions.
Encrypt the archive in off-server backup storage and preserve Vault recovery
material. The normal application backup does not include this separate database.
Also preserve each application's `keycloak-client` Secret in encrypted backup
storage if sessions must survive. After losing those Secrets, redeploy each
affected startup with `keycloak-client` before OAuth2 Proxy to apply the new
credentials. Users will need to sign in again.

### Recover authentication on a fresh cluster

Prepare the replacement cluster, then configure the owner project with the same
Keycloak namespace and public hostname, and pin compatible Keycloak and PostgreSQL
versions. Before the first Keycloak deploy, restore the matching snapshot from a
trusted, decrypted local archive:

```bash
startup ansible restore --working-directory deployment --environment production \
  --playbook roles/keycloak/files/restore-playbook.yml \
  --backup-dir /private/keycloak-backup \
  --db-file /private/keycloak-backup/startup-keycloak-backup-SNAPSHOT.tar.gz \
  --no-restore-media --yes
```

The restore recreates the recovery Secret, provisions dedicated PostgreSQL storage
and imports the database in one transaction. An existing Keycloak Deployment,
populated database, conflicting Secret or orphaned authentication volume blocks
recovery. Pause automatic application and Keycloak deployments for this target
until the restore completes.

After the restore succeeds, deploy the Keycloak service. Restore backed-up
application client Secrets where available and redeploy each affected startup so
its client and proxy are configured against the restored Keycloak. Verify
discovery and an actual login through each application before moving traffic.

`keycloak-client` creates the startup realm and a confidential client with exact
callback URLs, PKCE S256 and direct password grants disabled. It preserves users
when reapplied. An explicitly shared realm still gets a different client for each
startup. Existing credential-to-realm bindings cannot be silently reassigned.
Startup deploys configure clients, not the shared server.

New realms start with self-registration disabled. Manage this setting in the
Keycloak administration console: select the startup realm, then **Realm settings
→ Login → User registration** (`registrationAllowed`). Disabling self-registration
does not prevent existing users from logging in or administrators from creating
accounts. Subsequent deployments preserve the realm's registration policy.
Do not duplicate this identity setting in the application's administration page.
For an explicitly shared realm, registration applies to all its clients;
application-specific admission remains a separate authorization decision.

### Declarative session policy

Set `keycloak_realm_session_settings` in the startup's
`deployment/group_vars/all.yml` to manage selected realm settings on every
application deployment. Values are seconds. For seven days without activity and
a thirty-day maximum:

```yaml
keycloak_realm_session_settings:
  ssoSessionIdleTimeout: 604800
  ssoSessionMaxLifespan: 2592000
  clientSessionIdleTimeout: 0
  clientSessionMaxLifespan: 0
  ssoSessionIdleTimeoutRememberMe: 0
  ssoSessionMaxLifespanRememberMe: 0
```

Zero makes the client and Remember Me settings inherit the SSO limits. These six
fields are the supported keys. Unspecified fields remain operator-managed;
the default empty mapping preserves all session settings. Declared values take
precedence over subsequent admin-console edits when the startup is deployed.
Registration, users and other realm settings are not part of this update.
If several startups deliberately share one realm, give its session policy one
deployment owner and leave this mapping empty in the others.

Align OAuth2 Proxy's existing `oauth2_proxy_extra_args` with the policy, retaining
any other application arguments: `--cookie-expire=168h` persists the browser
session for seven days and `--cookie-refresh=1m` renews it during use. Keycloak
enforces the absolute maximum. Short-lived access tokens stay short-lived;
their validity is not the interactive login duration. Browser cookie deletion,
logout or account revocation can require login earlier.

Application-specific user/data permissions remain the application's concern.
Existing applications with their own login need an explicit migration plan.
Configure the server and startup binding before enabling the application's proxy.

Removing a startup does not remove Keycloak, its database or other realms. Cluster
removal requires deciding whether to migrate, stop, or explicitly delete the
installation and its data. The roles contain no automatic uninstall operation.

Validation: `tests/test_keycloak_runtime_integration.py` runs the actual client
tasks against real Keycloak/PostgreSQL, reapplies them after creating a user,
checks realm isolation, and logs into the rendered OAuth2 Proxy in Chromium.
It also executes the backup playbook and restores its database dump into a
separate PostgreSQL database to verify that the user survives.
This local test does not prove production DNS, ACME issuance or data migration.

## Existing installations

Retain the existing namespace, hostname, TLS mode, database placement and any
explicit cluster ID when adopting these roles. For an installation in
`legacy-auth`, every connected project must keep
`keycloak_namespace: legacy-auth` until a separate migration has been verified.
Changing a namespace creates a different installation; it does not move users,
Secrets, volumes or clients. Keep recoverable access and a verified backup
before changing an existing installation.

The first project deployment detects the existing Keycloak Deployment and leaves
it untouched. The owner project's lifecycle must not determine the lifetime of
shared authentication: transfer configuration and access to a successor project
before retiring the current owner.

The real-playbook tests in `tests/test_keycloak_project_integration.py` cover
installation, namespace isolation, both TLS modes, reuse, explicit owner updates
and denial of attached-project mutations. Their file-backed Kubernetes API is an
external-system stand-in; the runtime suite separately verifies the actual
Keycloak/PostgreSQL/proxy integration.
