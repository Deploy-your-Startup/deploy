# Keycloak realm and client provisioning

Declare realms and clients in the cluster owner's project. The owner deploys
the bindings after shared Keycloak is ready and the target application
namespaces exist. Add this to the owner service play after the `keycloak` role:

```yaml
- name: Provision declared startup authentication
  import_role:
    name: keycloak-client
  when: (auth_provider | default('')) == 'keycloak'
  tags: [never, keycloak-client]
```

Keep the owner service configuration and `startup_keycloak_operator: true` in
the owner's `deployment/group_vars/all.yml`, together with the declarations:

```yaml
keycloak_clients:
  - namespace: example-startup
    realm: example-startup
    client_id: example-startup
    origin: https://app.example.com
    passkeys_enabled: true
```

Apply with `startup ansible deploy --working-directory deployment --environment
production --service keycloak-client` from the owner project. The role uses the
shared administrator Secret and writes a `keycloak-client` Secret into each
declared application namespace. Each app's deployment reads its own Secret via
[OAuth2 Proxy](../oauth2-proxy/README.md#keycloak). It can keep its existing login
until the new integration is ready.

Use one client per namespace and a unique client ID within each realm. New
realms start with registration closed. Clients are confidential, require S256
PKCE, and use the exact callback `<origin>/oauth2/callback`. Reapplying the role
preserves existing users and secrets. Removing a declaration leaves the
existing realm, client, and credentials in place for explicit retirement.
The authentication namespace and master realm are reserved.

## Passkeys

Set `passkeys_enabled` on a client declaration. Its default is `null`, which
leaves the realm's passkey policy and registration action under operator control.

- `true` enables integrated passkey login, discoverable credentials and required
  user verification. It enables `webauthn-register-passwordless` without changing
  whether that action is mandatory by default. Fresh realms retain optional
  enrollment and password login. Existing authentication flows and operator
  requirements are preserved.
- `false` disables the integrated passkey login option. Registered credentials,
  users, password login and the remaining passwordless policy are retained.
- Returning to `null` stops managing these settings; it does not revert them.

Only declared passkey fields are reconciled. The role retains the existing RP
ID, mediation, registration policy, other required actions and other realms.
Keep the authentication hostname/RP ID stable after users register passkeys.
Users register their own passkeys in Keycloak's Account Console under account
security/signing in, or through a user-specific registration action. The device
chooses Face ID, Touch ID, a PIN or another supported verification method.
No biometric data is sent to the application.

A later console edit to a managed field is corrected by the next deployment.
Use `null` if the console should own the policy instead. Test enrollment,
password fallback and passkey login on the target browsers before rollout.

## Session policy

`session_settings` defaults to `{}`. Only explicitly declared
session durations are managed. Supported fields (seconds) are
`ssoSessionIdleTimeout`, `ssoSessionMaxLifespan`, `clientSessionIdleTimeout`,
`clientSessionMaxLifespan`, `ssoSessionIdleTimeoutRememberMe`, and
`ssoSessionMaxLifespanRememberMe`. SSO idle and maximum must be positive;
the other fields accept zero for Keycloak's inherited/default behavior.
