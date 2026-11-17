# Operational safeguards

What the shared roles do to keep routine runs from damaging a deployment.
Pinned versions live in each role's `defaults/main.yml`; this page describes
behaviour, not version numbers.

## Versions and upgrades

- New k3s installations use `k3s_version`; cert-manager uses
  `cert_manager_chart_version`. Existing clusters keep their versions until an
  explicit `startup ansible k3s-upgrade` or `cert-manager-upgrade`.
- Gateway API CRDs already managed by the cluster are preserved. An absent
  Gateway API is installed at `gateway_api_version`; a partial installation
  stops before overwriting any CRDs.
- Optional Hetzner charts (cloud controller, CSI, cert-manager DNS webhook) are
  pinned. They require provider-specific verification when enabled.
- Kubernetes modules use a pinned Python client in `/opt/startup-ansible`,
  leaving the distribution Python environment untouched.
- Changing `hetzner_os_image` affects only newly created servers. In-place
  release upgrades go through `startup ansible os-upgrade`; see the README.

## Ubuntu 26.04 containerd AppArmor compatibility

The `containerd-apparmor` role temporarily backports
[containerd PR 14487](https://github.com/containerd/containerd/pull/14487), also
[backported to 2.3](https://github.com/containerd/containerd/pull/14590).
Ubuntu forced profile stacking can prevent PostgreSQL processes from signalling
each other and hang startup. The backport adds only the exact stacked peer's
`signal` and `ptrace` rules to the containerd 2.3.4 default profile; AppArmor
stays enforcing and the kernel hardening setting is not changed.

On Ubuntu 26.04 with AppArmor enabled it runs before k3s installation/upgrades
and before `update-vms` changes packages. An OS upgrade to 26.04 also prepares
the persistent policy while still on Ubuntu 24.04; read-only compatibility
checks cover the entire node selection before the first node is upgraded. It
validates the profile with
the distribution parser, loads it without restarting workloads, and verifies
that it is enforcing. Repeated runs reload it to recover from partial failures.
The profile in `/etc/apparmor.d/` persists across reboots. It supports the
reviewed default rootful k3s runtime with containerd 2.3.4, 2.3.6 or 2.3.7
(their upstream profiles are identical). Fresh installations require a reviewed
k3s target; unknown runtime versions or a disabled AppArmor boot service stop
before profile changes. Custom daemon confinement, RootlessKit,
missing distribution includes and custom persistent profiles stop maintenance
without replacing that policy. Custom policies need an operator-reviewed merge
of the upstream rules. Other distributions and explicitly disabled AppArmor are
left alone.

A release and `startup update` adoption are required before existing users can
run these tasks. Refresh roles with `startup ansible setup_ansible`, then run
an approved maintenance operation; editing a project's pin alone installs no
host policy. Hosts already protected by the identical hotfix are adopted. A database process
already stuck before the correction may need a separately coordinated restart;
loading this policy does not restart workloads.

Remove this role and its installation/maintenance imports after a qualified
stable k3s release embeds the upstream fix. On upgraded nodes, remove only our
byte-identical persistent profile as part of that migration; preserve custom
profiles. Coordinate a k3s restart so the native profile is loaded, then verify
PostgreSQL startup, a database query, node reboot and enforcement. Removing the
repository files alone does not remove an already installed host profile.
The disposable-node tests use the real Ubuntu parser and Ansible tasks, with
kernel loading simulated; they do not establish forced-stacking enforcement on
a real Ubuntu 26.04 VM.

## Servers

- `hetzner-server` creates servers with `force: true`, which lets the module
  resize a server by stopping it. A preflight refuses any run whose
  `server_type` differs from a running server. Pass `-e allow_server_resize=true`
  for one run only when the resize is intended; a rejected resize leaves the
  server stopped.
- Lowering `master_count` needs `allow_master_teardown`. Surplus workers are
  preserved unless the CLI is called with `--allow-worker-teardown`, which asks
  for confirmation (`--yes` confirms it for automation). Failed drains stop
  deletion; unmanaged pods or emptyDir data block a drain until resolved.

## Secrets

- Kubernetes Secrets are applied with task output and diffs censored. k3s
  installation tokens are passed through the environment in censored tasks.
- Tailscale OAuth secrets are exchanged on the controller. New nodes receive
  distinct single-use auth keys that expire after ten minutes; already joined
  nodes request none. A supplied `tskey-auth-` key keeps its own expiry and reuse
  policy; prefer OAuth for repeatable provisioning. Nodes provisioned before this
  exchange may still hold OAuth secrets in Cloud-init state.

## Data

- Projects may override the Postgres image; apply only patch updates within the
  existing major. Backups run once per cluster and are stored with owner-only
  permissions.
- Postgres records its storage node on the PV and constrains scheduling to that
  node. Existing placement is adopted. New multi-node databases require
  `postgres_storage_node`; conflicting placement or an absent data node stops the
  deployment. This is local storage without failover: node replacement needs a
  verified backup and restore.

## Network and DNS

- DNS zones are resolved with a pinned Public Suffix List, including suffixes
  such as `co.uk`. Existing delegated zones are preferred; `dns_zone_overrides`
  selects an explicit zone. Invalid domains or unrelated overrides stop before
  any DNS write.
- SSH accepts new host keys and rejects changed ones. The public Kubernetes API
  is closed by default (`firewall_api_source_ips: []`); clients reach it through
  the SSH tunnel from `startup ansible kubeconfig`. SSH stays public for hosted
  CI runners; restrict `firewall_ssh_source_ips` for fixed runners.
- Set `cert_manager_acme_email` to an operator-owned address if ACME contact
  notifications are wanted. No maintainer address is supplied.
