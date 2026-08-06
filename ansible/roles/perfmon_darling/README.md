# perfmon_darling

Configures the Darling collector service: renders its `darling.json` config file and reconciles
which SQL Server instances it collects from in the central store's monitored-server registry.

## What it does

1. Validates `perfmon_darling_pg_password` is set - the store connection the service writes
   collected data into.
2. Validates SQL-auth credentials (username + password) are set for every instance whose resolved
   auth mode is `sql`.
3. Renders `darling.json` to `perfmon_darling_config_path`, listing every monitored instance's
   connection settings. On Linux there is no DPAPI blob to persist SQL-auth secrets in, so the
   service backfills a registry row's password from this file on every start by matching storage
   name + auth + username - every SQL-auth instance must stay listed here whether or not the
   registry already knows about it.
4. Restarts `perfmon_darling_service_name` when `darling.json` changes, if
   `perfmon_darling_manage_service` is set.
5. Reconciles `config.config_monitored_servers` in the store (see Registry reconciliation below)
   when `perfmon_darling_register_new` or `perfmon_darling_prune_orphaned` is set.

## Requirements

- `perfmon_darling_pg_password`: password for the store's collector role (this role writes/owns
  data; it is a different role than the Grafana datasource's read-only `viewer` role documented in
  `perfmon_grafana`'s README). Supply via vault.
- `perfmon_darling_sql_username` / `perfmon_darling_sql_password` (or the per-instance
  `darling_sql_username` / `darling_sql_password`): required for every instance whose resolved
  auth mode is `sql`.
- Ansible collection: `community.postgresql` (registry reconciliation runs SQL against the store
  directly).

## Usage

### Minimal playbook

The role runs on the host where the Darling collector service is installed:

```yaml
- name: Configure the Darling collector service
  hosts: darling
  gather_facts: false
  roles:
    - perfmon_darling
```

Run it:

```bash
ansible-playbook -i ansible/inventory/hosts.yml ansible/playbooks/deploy_perfmon_darling.yml
```

Or run the full end-to-end playbook (also deploys Grafana dashboards and alerting):

```bash
ansible-playbook -i ansible/inventory/hosts.yml ansible/playbooks/main.yml
```

### Inventory

Monitored instances are drawn from the same `sql_servers` group the `perfmon_grafana` role reads,
so one inventory drives both roles:

```yaml
all:
  children:
    sql_servers:
      hosts:
        sql01:
          ansible_host: pubs-dev.example.com
    darling:
      hosts:
        darling-collector:
          ansible_host: darling-host.example.com
```

Per-instance settings are ordinary keys on a `sql_servers` host, or on an entry when overriding
`perfmon_darling_instances` directly:

| Key | Default | Notes |
|---|---|---|
| `darling_name` | `inventory_hostname` | Display name in the registry and in dashboards' `$server` dropdown. |
| `ds_host` | `ansible_host` | Host the collector connects to. Also what `perfmon_grafana`'s alert rules match `server_id` against. |
| `mssql_database` | - | Set for Azure SQL Database, so databases on one logical server get distinct `server_id`s. |
| `darling_auth` | `perfmon_darling_auth` (`sql`) | `sql` or `integrated` (Kerberos). |
| `darling_sql_username` / `darling_sql_password` | `perfmon_darling_sql_username` / `perfmon_darling_sql_password` | Required when the resolved auth is `sql`. |
| `darling_trust_server_certificate` | `true` | |
| `darling_encrypt_mode` | `Mandatory` | |
| `darling_read_only_intent` | `false` | Appends `:RO` to the storage name so a read-only replica gets its own `server_id`, distinct from the primary. |
| `darling_excluded_databases` | `[]` | |
| `darling_monthly_cost_usd` | `0` | Feeds the FinOps dashboards' cost-allocation panels. |

## Variables

| Variable | Default | Notes |
|---|---|---|
| `perfmon_darling_config_path` | `/etc/darling/darling.json` | Where the rendered config is written. The service resolves `DARLING_CONFIG`, then `darling.json` beside the binary. |
| `perfmon_darling_config_owner` / `perfmon_darling_config_group` | `root` / `root` | Ownership of the rendered config file. |
| `perfmon_darling_instances` | derived from `sql_servers` group | Override with an explicit list when your inventory group is named differently or you are running without a `sql_servers` group. |
| `perfmon_darling_pg_host` / `perfmon_darling_pg_port` / `perfmon_darling_pg_database` | `darling-pg` / `5432` / `darling` | Store the service collects into. |
| `perfmon_darling_pg_user` | `darling` | The service's own store role - separate from `perfmon_grafana`'s `perfmon_darling_pg_user` (default `viewer`), which is read-only. |
| `perfmon_darling_pg_password` | - | Required. Password for `perfmon_darling_pg_user`. |
| `perfmon_darling_auth` | `sql` | Fleet-wide default auth mode; override per instance with `darling_auth`. |
| `perfmon_darling_capture_plans` | `true` | darling.json `capturePlans`. |
| `perfmon_darling_alerts_enabled` | `false` | darling.json `alerts.enabled` - the service's own alert engine, separate from the Grafana-side alert rules `perfmon_grafana` provisions. |
| `perfmon_darling_analysis_enabled` | `true` | darling.json `analysis.enabled`. |
| `perfmon_darling_register_new` | `true` | Insert instances missing from the registry - see Registry reconciliation. |
| `perfmon_darling_prune_orphaned` | `false` | Disable registry rows whose instance left the inventory. Off by default: a partial-inventory run must not disable another team's instance. Only set `true` for a run whose inventory is the complete, current fleet. |
| `perfmon_darling_manage_service` | `false` | Whether to restart `perfmon_darling_service_name` when `darling.json` changes. Off by default - the demo stack runs the service as a container, and a host install may manage its own lifecycle. |
| `perfmon_darling_service_name` | `darling` | Service unit name, when `perfmon_darling_manage_service` is set. |

## Registry reconciliation

`config.config_monitored_servers` is the store's own record of which instances to collect from and
how to connect to them. `darling.json`'s `servers[]` list only seeds this table on the store's
first start, while it's still empty - an instance added to inventory afterward is otherwise never
picked up. This role's registry tasks (`tasks/registry.yml`) insert it directly instead:

1. Read the current registry.
2. For each instance in `perfmon_darling_instances` not already present (by `server_id`), insert a
   row and enable it. `server_id` is derived from the instance's storage name (host, plus database
   for Azure SQL, plus a `:RO` suffix for read-only intent) with the same deterministic hash
   (`darling_server_id` in `filter_plugins/darling.py`, mirroring upstream's
   `ServerIdHelper.GetDeterministicHashCode`) the running service uses - so a row inserted here is
   the row the service collects into. Skipped when `perfmon_darling_register_new` is false.
3. When `perfmon_darling_prune_orphaned` is set, disable (never delete) any enabled row whose host
   isn't in this run's `perfmon_darling_instances` - collected history survives a disable.

The table has a trigger that bumps `config_version` on change, so both an insert and a disable take
effect on the running service without a restart. Only a `darling.json` content change (credentials,
auth mode, toggles) needs the `Restart darling` handler.

## Connection strings and secrets

SQL-auth secrets live only in `darling.json`, not in the registry table - there is no DPAPI blob to
persist them into on Linux. The rendered file must keep listing every SQL-auth instance's
`username`/`password` on every run so the service can backfill a registry row's secret by matching
name + auth + username, even for instances the registry has known about for a while.

`perfmon_darling_pg_password` in this role's connection string is the store's collector role,
distinct from the read-only `viewer` role `perfmon_grafana`'s Postgres datasource authenticates as
- the two roles typically run against the same store but authenticate as different Postgres users
with different privileges.
