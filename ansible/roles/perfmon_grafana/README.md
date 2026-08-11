# perfmon_grafana

Provisions the Darling datasource, dashboards, and alert rules for PerformanceMonitor in Grafana.

## What it does

1. Generates dashboard JSON files from the Python builder embedded in the role
   (`files/build-darling-dashboards.py`, panel builders in `files/darling_defs/`) and writes them
   to `files/grafana/dashboards/darling/`.
2. Creates or updates a single PostgreSQL datasource (`perfmon_darling_ds_name` /
   `perfmon_darling_ds_uid`) pointing at the central Darling store. Dashboards select an instance
   with the `$server` template variable, populated by a live query against
   `config.config_monitored_servers` in the store.
3. Creates the Darling folder in Grafana (`grafana_darling_folder` / `grafana_darling_folder_uid`),
   then imports all dashboard JSON files into it.
4. Provisions Grafana Unified Alerting rule groups per monitored instance via the Grafana
   Provisioning API. Every rule reads through the single Darling datasource, filtering by the
   instance's `server_id` in the store. Rule groups are shared by name across instances, so each
   run merges its own instances' rules into the group.
5. Provisions contact points (email, Slack, PagerDuty, or webhook) via the Grafana Provisioning API
   when a delivery backend is configured.
6. Provisions mute timings listed in `perfmon_alert_mute_timings` via the Grafana Provisioning API.
7. Upserts a `team=perfmon` route in the Grafana notification policy via `upsert_notification_route.py`.
   Only the perfmon route is touched; all other routes in the policy tree are left untouched.

## Requirements

- `grafana_api_key`: a Grafana service account token with Admin role. Create the service account
  in the Grafana UI (Administration -> Service accounts) and supply the token via vault.
- `perfmon_darling_pg_password`: password for the Darling store's read-only `viewer` role. Supply
  via vault.
- Grafana must have Unified Alerting enabled. Add `GF_UNIFIED_ALERTING_ENABLED=true` to Grafana's
  environment.
- Ansible collection: `community.grafana`.

## Usage

### Minimal playbook

The role runs on the Grafana host:

```yaml
- name: Deploy PerformanceMonitor dashboards to Grafana
  hosts: grafana
  gather_facts: false
  tasks:
    - name: Apply perfmon_grafana role
      ansible.builtin.import_role:
        name: perfmon_grafana
```

Run it:

```bash
ansible-playbook -i ansible/inventory/hosts.yml ansible/playbooks/deploy_perfmon_grafana.yml
```

Or run the full end-to-end playbook:

```bash
ansible-playbook -i ansible/inventory/hosts.yml ansible/playbooks/main.yml
```

### Tag-based targeting

Use tags to run specific operations without executing the full role:

```bash
# Regenerate dashboard JSON files only
ansible-playbook -i ansible/inventory/hosts.yml ansible/playbooks/deploy_perfmon_grafana.yml --tags generate

# Generate and import dashboards
ansible-playbook -i ansible/inventory/hosts.yml ansible/playbooks/deploy_perfmon_grafana.yml --tags dashboards

# Provision the datasource only
ansible-playbook -i ansible/inventory/hosts.yml ansible/playbooks/deploy_perfmon_grafana.yml --tags datasources

# Provision alerting resources only
ansible-playbook -i ansible/inventory/hosts.yml ansible/playbooks/deploy_perfmon_grafana.yml --tags alerting
```

Available tags:

| Tag | Tasks covered |
|---|---|
| `datasources` | Create/update the Darling datasource |
| `dashboards` | Generate JSON files and import them into Grafana |
| `generate` | Generate JSON files only (subset of `dashboards`) |
| `alerting` | Alert rules, contact points, mute timings, notification policy |
| `teardown` | Remove all provisioned resources (see Removal below) |
| `teardown_alerting` | Remove alert rules, contact points, mute timings, notification policy route |
| `teardown_datasources` | Remove the Darling datasource |
| `teardown_dashboards` | Remove the Darling folder and all dashboards inside it |

### Inventory

Alert rule groups are provisioned per SQL Server instance, one rule set per instance in a
`sql_servers` group, plus a `grafana` group for the Grafana host:

```yaml
all:
  children:
    sql_servers:
      hosts:
        sql01:
          ansible_host: pubs-dev.example.com
        sql01-reporting:
          ansible_host: pubs-dev-reporting.example.com
    grafana:
      hosts:
        grafana:
          ansible_host: grafana-dev.example.com
          grafana_url: http://grafana-dev.example.com:3000
```

Each instance's alert rules resolve to a `server_id` by matching `ds_host` (or `ansible_host` when
`ds_host` is unset) against `host` in the store's `config.config_monitored_servers` table - the
same host value the Darling collector registers instances under. Use `ds_host` when the collector
registered an instance under a different address than the one this Ansible control node uses:

```yaml
sql_servers:
  hosts:
    mssql-a:
      ansible_host: localhost   # address this control node uses
      ds_host: mssql-a          # host value the collector registered this instance under
```

### Required credentials

`grafana_api_key` and `perfmon_darling_pg_password` have no role defaults and must be supplied.
The recommended way is Ansible Vault:

```yaml
# group_vars/grafana.yml
grafana_api_key: "{{ vault_grafana_api_key }}"
perfmon_darling_pg_password: "{{ vault_perfmon_darling_pg_password }}"
```

Create the service account in the Grafana UI (Administration -> Service accounts) with Admin role.
`perfmon_darling_pg_password` is the password for the Darling store's `viewer` role, provisioned by
`Darling/tools/provision-roles.sql` in the upstream project.

## Variables

### Core variables

| Variable | Default | Notes |
|---|---|---|
| `grafana_url` | `http://localhost:3000` | Grafana base URL. |
| `grafana_api_key` | - | Required. Grafana service account token with Admin role. |
| `grafana_darling_folder` | `PerformanceMonitor (Darling)` | Grafana folder title where dashboards are placed. |
| `grafana_darling_folder_uid` | `perfmon-darling` | Grafana folder UID. Must be stable across runs. |
| `perfmon_darling_ds_name` | `Darling` | Datasource display name. |
| `perfmon_darling_ds_uid` | `darling` | Datasource UID. Must be stable across runs - dashboards and alert rules reference it directly. |
| `perfmon_darling_pg_host` | `darling-pg` | Hostname of the Darling store. |
| `perfmon_darling_pg_port` | `5432` | Port of the Darling store. |
| `perfmon_darling_pg_database` | `darling` | Database name on the Darling store. |
| `perfmon_darling_pg_user` | `viewer` | Read-only role Grafana's datasource authenticates as. |
| `perfmon_darling_pg_password` | - | Required. Password for `perfmon_darling_pg_user`. |
| `perfmon_darling_pg_sslmode` | `disable` | Postgres SSL mode for the datasource connection. |
| `perfmon_instances` | derived from `sql_servers` group | Override with an explicit list when your inventory group is named differently or you are running without a `sql_servers` group. See below. |
| `perfmon_prune_orphaned` | `false` | Delete dashboards, mute timings, and alert rules that exist in Grafana but aren't produced by this run - dashboards/mute timings no longer generated or configured, alert rules for an instance no longer in `perfmon_instances`. Off by default so a run against a partial inventory never deletes another instance's alert rules. Only set `true` for a run whose inventory is the complete, current fleet - see Retiring an instance. |

### Alert threshold variables

All thresholds default to the values from the upstream `DarlingAlertSettings.cs`. Override per-host
in `host_vars/` or per-group in `group_vars/` without modifying provisioning files.

| Variable | Default | Alert rule |
|---|---|---|
| `perfmon_alert_cpu_pct` | `80` | High CPU - fires when the latest collected CPU sample >= this percent |
| `perfmon_alert_blocking_count` | `1` | Blocking Detected - fires when captured blocking events in the last hour >= this value |
| `perfmon_alert_deadlock_count` | `1` | Deadlocks Detected - fires when deadlock count in the last 5 minutes >= this value |
| `perfmon_alert_tempdb_pct` | `80` | TempDB Space - fires when used >= this percent of allocated TempDB |
| `perfmon_alert_disk_free_pct` | `10` | Low Disk Space - fires when free space on any volume < this percent (OR condition with GB floor) |
| `perfmon_alert_disk_free_gb` | `5` | Low Disk Space - fires when free space on any volume < this many GB (OR condition with pct floor) |
| `perfmon_alert_query_duration_floor_min` | `30` | Long-Running Query - fires when any currently executing query has been running >= this many minutes |
| `perfmon_alert_poison_wait_floor_ms` | `500` | Poison Wait - fires when avg ms per wait event for `THREADPOOL`, `RESOURCE_SEMAPHORE`, or `RESOURCE_SEMAPHORE_QUERY_COMPILE` >= this value |
| `perfmon_alert_long_running_job_multiplier` | `3` | Long-Running Job - fires when any running SQL Agent job's duration >= this multiple of its average |
| `perfmon_alert_failed_job_lookback_min` | `60` | Failed Job - how far back to look for failed SQL Agent job runs |
| `perfmon_alert_collection_stale_min` | `30` | Collection Stopped - fires when no collector has logged a run for an instance in this many minutes |

### Alert routing variables

These control the timing behaviour of the `team=perfmon` notification policy route. The defaults
match the upstream `UserPreferences.cs` grouping cadence.

| Variable | Default | Notes |
|---|---|---|
| `perfmon_alert_group_wait` | `30s` | How long to wait before sending the first notification for a new group of alerts. |
| `perfmon_alert_group_interval` | `5m` | How long to wait before sending a notification about new alerts added to an already firing group. |
| `perfmon_alert_repeat_interval` | `4h` | How long to wait before re-sending a notification for an alert that is still firing. |
| `perfmon_alert_group_by` | `team,instance,alertname` | Comma-separated list of label names used to group alerts into notifications. |

### Mute timing variables

| Variable | Default | Notes |
|---|---|---|
| `perfmon_alert_mute_timings` | `[]` | List of Grafana mute timing objects to provision. Each entry must have a `name` (use `perfmon-` prefix) and `time_intervals`. When `perfmon_prune_orphaned` is set, timings with a `perfmon-` prefix that are no longer in this list are removed. |

### Alert contact point variables

| Variable | Default | Notes |
|---|---|---|
| `perfmon_alert_contact_points` | `[]` | List of contact point objects passed to the Grafana Provisioning API. Each entry needs `uid`, `name`, `type`, and `settings`. All entries should use the same `name` value (`perfmon_alert_receiver_name`) so Grafana treats them as one receiver with multiple integrations. Set `state: absent` on an entry to remove it. |
| `perfmon_alert_receiver_name` | `perfmon-alerts` | Name of the receiver the notification policy route points to. Must match the `name` field on every contact point entry. |

### Overriding the instance list

By default the role builds `perfmon_instances` by extracting the full `hostvars` dict for every
host in the `sql_servers` inventory group. Each element therefore exposes the same keys as the
host's inventory entry: `inventory_hostname`, `ansible_host`, `ds_host`, etc.

Override the variable when your group has a different name or you are running without an inventory:

```yaml
perfmon_instances:
  - inventory_hostname: pubs-dev01
    ansible_host: pubs-dev01.example.com
  - inventory_hostname: pubs-dev02
    ansible_host: pubs-dev02.example.com
    ds_host: pubs-dev02-internal.example.com
```

## Alerting

The role provisions Grafana Unified Alerting rule groups per SQL Server instance via the Grafana
Provisioning HTTP API, covering the alerts in `DarlingAlertSettings.cs` / `AlertEngine.cs`
(ported in `rules-instance-darling.yaml.j2`). Rules evaluate every minute, with a pending period
before firing (1 minute for most rules; 5 minutes for TempDB, Low Disk).

### Enabling a contact point

```yaml
# e.g. in group_vars/grafana.yml
perfmon_alert_contact_points:
  - uid: perfmon-slack
    name: perfmon-alerts
    type: slack
    settings:
      url: https://hooks.slack.com/services/T000000/B000000/XXXXXXXXXXXXXXXXXXXXXXXX
      recipient: "#alerts"
      title: "PerfMon Alert"
  - uid: perfmon-pagerduty
    name: perfmon-alerts
    type: pagerduty
    settings:
      integrationKey: abc123def456abc123def456abc123de
```

Re-run `deploy_perfmon_grafana.yml`. The role provisions the contact point and the notification policy
route points to `perfmon_alert_receiver_name` (default `perfmon-alerts`). Add multiple entries
to the list to fire more than one integration for every alert.

### Configuring mute timings

```yaml
# e.g. in group_vars/grafana.yml
perfmon_alert_mute_timings:
  - name: perfmon-weekend-maintenance
    time_intervals:
      - weekdays: ["saturday", "sunday"]
        times:
          - start_time: "19:00"
            end_time: "07:00"
```

Name each mute timing with a `perfmon-` prefix to avoid colliding with timings owned by other teams.
With `perfmon_prune_orphaned` set, an `alerting` tag run removes any `perfmon-` timing no longer in
this list.

### Notification policy

The role upserts a single `team=perfmon` child route via `upsert_notification_route.py`. The script
reads the current policy tree, removes any prior `team=perfmon` route, appends a fresh one, and
writes it back. All other routes are left untouched, so this role is safe to use alongside other
teams managing their own routes in the same Grafana instance.

### Unreachable data

If the Darling store itself becomes unreachable, every alert rule query fails and each rule goes
to **Error** state in the Alerts UI, since all of them read through the single shared datasource.
A single instance falling behind is covered by the Collection Stopped alert rather than a Grafana-side connectivity error - see
`perfmon_alert_collection_stale_min`.

## Removal

### Full teardown

Remove everything this role has provisioned from a Grafana instance:

```bash
ansible-playbook -i ansible/inventory/hosts.yml ansible/playbooks/deploy_perfmon_grafana.yml --tags teardown
```

Teardown order: notification policy route, mute timings, alert rule groups, contact points,
Darling datasource, Darling folder (folder deletion cascades to all dashboards inside it).

Use sub-tags for selective removal:

```bash
ansible-playbook -i ansible/inventory/hosts.yml ansible/playbooks/deploy_perfmon_grafana.yml --tags teardown_alerting
ansible-playbook -i ansible/inventory/hosts.yml ansible/playbooks/deploy_perfmon_grafana.yml --tags teardown_datasources
ansible-playbook -i ansible/inventory/hosts.yml ansible/playbooks/deploy_perfmon_grafana.yml --tags teardown_dashboards
```

### Retiring an instance

Remove the host from inventory and re-run with `perfmon_prune_orphaned=true` (full playbook, or
`--tags alerting`), using an inventory that's the complete, current fleet - not a partial one:

```bash
ansible-playbook -i ansible/inventory/hosts.yml ansible/playbooks/deploy_perfmon_grafana.yml \
  --tags alerting -e perfmon_prune_orphaned=true
```

This drops the retired instance's rules from each shared rule group. There is no per-instance
datasource to prune - all instances share the one Darling datasource. To stop the instance from
appearing in the `$server` dropdown and being collected at all, disable or remove its row in
`config.config_monitored_servers` via the `perfmon_darling` role (see that role's registry
reconciliation task).

Without `perfmon_prune_orphaned`, alert rules are left in place - a run against a partial
inventory has no way to distinguish "this instance was retired" from "this instance just isn't in
this particular run's scope". If an inventory is accidentally omitted from a run, nothing is lost:
re-run against the full fleet and every alert rule is recreated.
