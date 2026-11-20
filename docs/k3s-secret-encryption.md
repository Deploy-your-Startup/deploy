# K3s Secret encryption at rest

K3s can encrypt Kubernetes Secret objects before writing them to the cluster
datastore. Applications continue to use the same Secret references and access
controls. Ansible Vault continues to protect the source credentials.

## New clusters

The proposed shared role default is `secrets-encryption: true` for new
clusters, using K3s-generated keys and its default `aescbc` provider. Keep
the setting in persistent K3s server configuration so upgrades preserve it.
Joining servers must match the target cluster's effective setting, including
when joining an existing unencrypted cluster. Routine infrastructure runs
must preserve the encryption state of an installed server.

[K3s encryption configuration](https://docs.k3s.io/security/secrets-encryption)
· [K3s server options](https://docs.k3s.io/cli/server)

## Existing clusters

Use a dedicated maintenance window. Confirm the running K3s version, datastore,
server count and application dependencies first. The documented in-place
activation was introduced in `v1.33.10+k3s1`, `v1.34.6+k3s1` and
`v1.35.3+k3s1` in their respective release lines. Upgrade older clusters
separately and confirm health before migrating.

1. Take and verify a recovery point. Use a K3s snapshot for embedded etcd,
   a consistent copy of `server/db` for SQLite, or the external datastore's
   native backup procedure. Back up application databases, media and local
   volumes separately. Save the matching K3s server token and startup
   configuration. Restore the recovery point in an isolated environment and
   record how long it takes.
2. For a cluster started without encryption, run the appropriate K3s
   single-server or HA procedure. Run `k3s secrets-encrypt enable` on the
   first server, persist the encryption setting and restart the servers.
   Confirm stage `start` and matching server hashes. Run
   `k3s secrets-encrypt rotate-keys` to rewrite existing Secret objects,
   wait for completion, and perform the second restart round. Maintain
   quorum during HA restarts. A single-server cluster loses API availability
   during both restarts.
3. Confirm `Enabled`, `reencrypt_finished`, and matching server hashes.
   Verify the expected encryption prefix on current Secret records in the
   datastore, including one created before migration and one created after.
   Report only the verification result. Check API readiness, workloads, and
   a representative job that consumes a Secret.
4. Stop at an unexpected stage, hash mismatch, or persistent readiness or
   decryption error. If the migration cannot be recovered, restore the tested
   recovery point with its token and startup configuration. Determine which
   changes since that point would need replaying. Simply removing the
   setting can make already encrypted Secrets unreadable.

[K3s activation and status](https://docs.k3s.io/cli/secrets-encrypt#enable-secrets-encryption-on-an-existing-cluster)
· [K3s datastore backup and restore](https://docs.k3s.io/datastore/backup-restore)
· [Kubernetes datastore verification](https://kubernetes.io/docs/tasks/administer-cluster/encrypt-data/#verify-that-newly-written-data-is-encrypted)

## Recovery on a replacement server

An embedded etcd snapshot contains the Secret encryption configuration and
keys protected by the server token. Store the snapshot off-cluster and keep
the matching token separately in a restricted credential store. Keep older
tokens while retaining snapshots that require them. Before piloting an
existing cluster, demonstrate a restore of a pre-migration snapshot and an
encrypted snapshot onto a fresh test server. Verify that a workload can
consume a restored Secret and read its restored application data.

Rebuilding a fresh cluster from deployment configuration and application
backups remains an option. The new cluster generates its own key and the
deployment recreates Secrets from their configured sources. Values generated
only at runtime still need a separate recovery source or reissuance.

[K3s restore to a new host](https://docs.k3s.io/cli/etcd-snapshot#restoring-to-new-hosts)
· [K3s token rotation and older snapshots](https://docs.k3s.io/cli/token#k3s-token-rotate)
