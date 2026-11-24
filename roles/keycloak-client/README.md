# Keycloak client and realm configuration

Bind a startup to an existing Keycloak service and administrator recovery Secret
named `keycloak` in `keycloak_namespace` (default `authentication`). This role
does not install or upgrade the shared server. Tested with Keycloak 26.8.1;
the integrated passkey login forms require a compatible Keycloak version and
the standard browser authentication flow.

```yaml
project_name: example-startup
k8s_namespace: example-startup
base_domain: app.example.com
keycloak_realm: example-startup
keycloak_client_id: example-startup
keycloak_realm_passkeys_enabled: true
```

Include `keycloak-client` in the project's playbook after the shared service
exists. It creates a realm with registration closed only when absent, preserves
its users and existing startup client credentials, and configures a confidential
OIDC client with PKCE S256 and the callback `https://<base_domain>/oauth2/callback`.
OAuth2 Proxy can reuse the resulting `keycloak-client` Secret.
The application namespace must differ from the authentication namespace; the
master realm cannot be managed as a startup realm.

## Passkeys

`keycloak_realm_passkeys_enabled` is an Ansible variable, not a Keycloak container
environment variable. Its default is `null`, which leaves the realm's passkey
policy and registration action under operator control.

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

`keycloak_realm_session_settings` defaults to `{}`. Only explicitly declared
session durations are managed. Supported fields (seconds) are
`ssoSessionIdleTimeout`, `ssoSessionMaxLifespan`, `clientSessionIdleTimeout`,
`clientSessionMaxLifespan`, `ssoSessionIdleTimeoutRememberMe`, and
`ssoSessionMaxLifespanRememberMe`. SSO idle and maximum must be positive;
the other fields accept zero for Keycloak's inherited/default behavior.
