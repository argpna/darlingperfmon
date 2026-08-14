# perfmon_darling

Configures the Darling collector service: renders its `darling.json` config file and reconciles
which SQL Server instances it collects from in the central store's monitored-server registry.

## What the role does

1. Validates `perfmon_darling_pg_password` is set. Required by collector service to write data
   into Postgres/TimescaleDB.
2. Validates SQL-auth credentials are set for every instance when resolved auth mode is `sql`.
3. Renders `darling.json` from `perfmon_darling_config_path`. This role's registry reconciliation
   does not write SQL-auth passwords into the store, so the service backfills a registry row's
   password from this file on every collector start. Every SQL-auth instance must be listed here
   whether or not the registry already knows about it.
4. Restarts `perfmon_darling_service_name` when `darling.json` changes, if
   `perfmon_darling_manage_service` is set.
5. Reconciles `config.config_monitored_servers` in the store (see Registry reconciliation below)
   when `perfmon_darling_register_new` or `perfmon_darling_prune_orphaned` is set.

## Requirements

- `perfmon_darling_pg_password`: password for the store's collector role. Supply via vault.
- `perfmon_darling_sql_username` / `perfmon_darling_sql_password`: required for every instance whose
   resolved auth mode is `sql`.
- Ansible collection: `community.postgresql`

> [!NOTE]
> Define `darling_sql_username` / `darling_sql_password` if you have to override a
> fleet-wide user/pass (`perfmon_darling_sql_username`/ `perfmon_darling_sql_password`).

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

Run it using:

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
# e.g. inventory/hosts.yml
all:
  children:
    sql_servers:
      hosts:
        sql01-dev:
          ansible_host: sql01-dev.example.com
        sql02-dev:
          ansible_host: sql02-dev.example.com
    darling:
      hosts:
        darling-collector:
          ansible_host: darling-host.example.com
```

Per-instance settings are ordinary keys on a `sql_servers` host, or on an entry when overriding
`perfmon_darling_instances` directly:

```yaml
# e.g. inventory/host_vars/sql01-dev.yml
darling_sql_username: darling_app
darling_sql_password: "{{ vault_darling_app_password }}"
```

| Key | Default | Notes |
|---|---|---|
| `darling_name` | `inventory_hostname` | Display name in the registry and in dashboards' `$server` dropdown. |
| `ds_host` | `ansible_host` | Host the collector connects to; also hashed into `server_id`, the id `perfmon_grafana` role uses to identify an instance. |
| `mssql_database` | - | Database to connect to, if required (Azure SQL for e.g.), initial catalog. |
| `darling_auth` | `perfmon_darling_auth` (`sql`) | `sql` or `integrated` (Kerberos). |
| `darling_sql_username` / `darling_sql_password` | `perfmon_darling_sql_username` / `perfmon_darling_sql_password` | Required when the resolved auth is `sql`. |
| `darling_trust_server_certificate` | `true` | |
| `darling_encrypt_mode` | `Mandatory` | |
| `darling_read_only_intent` | `false` | Sets `ApplicationIntent=ReadOnly` on the collector's connection and appends `:RO` to the storage name for a distinct `server_id`. AG read-only routing requires `ds_host` to be the AG listener and `mssql_database` to participate in an AG. |
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
| `perfmon_darling_manage_service` | `false` | Whether to restart `perfmon_darling_service_name` (a systemd unit) when `darling.json` changes. See Restarting the collector below. |
| `perfmon_darling_service_name` | `darling` | Systemd unit name, when `perfmon_darling_manage_service` is set. |

## Registry reconciliation

`config.config_monitored_servers` is the store's own record of which instances to collect from and
how to connect to them. `darling.json`'s `servers[]` list only seeds this table on the store's
first start, while it's still empty - an instance added to inventory afterward is otherwise not
picked up. This role's registry tasks (`tasks/registry.yml`) insert it directly instead:

1. Read the current registry.
2. For each instance in `perfmon_darling_instances` not already present, insert a row and
   enable it. `server_id` is derived from the instance's storage name (see `filter_plugins/darling.py`)
   Skipped when `perfmon_darling_register_new` is false.
3. When `perfmon_darling_prune_orphaned` is set, it disables any enabled row whose host isn't listed
   in the current run's `perfmon_darling_instances`.

The table has a trigger that bumps `config_version` on change, so both an inserts and updates take
effect on the running service without a restart. Only a `darling.json` content change (credentials, auth
mode, toggles) needs a restart - see Restarting the collector below.

On a store the collector hasn't started yet, `config.config_monitored_servers` doesn't exist.
Reconciliation is skipped in that case. The collector's own first-start `servers[]` seeding
covers it, and the next run against an inventory change finds the table and reconciles in the
normal fashion.

## Restarting the collector

A new SQL-auth instance requires a collector service restart before it can be monitored. The registry
row is picked up live (see above), but the collector backfills the SQL-auth password by matching
`darling.json`'s `servers[]` list against the configuration it loaded once at process startup. A
running process does not re-read that file, so a password added after startup cannot be matched until
the collector restarts and re-parses the configuration (collector service behavior).

Integrated-auth instances have no password to backfill, so they do not require a restart.
