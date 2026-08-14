# Grafana dashboards for Erik Darling's PerformanceMonitor

Grafana front-end for [erikdarlingdata/PerformanceMonitor](https://github.com/erikdarlingdata/PerformanceMonitor),
Darling edition.

Darling is a headless collector service that polls one or more SQL Server instances and writes
into a central PostgreSQL/TimescaleDB store. The Grafana dashboards read that store through a
single Postgres datasource that serves every monitored instance.

Screenshots of all dashboards can be viewed at: [screenshot-gallery/darlingperfmon](https://argpna.github.io/screenshot-gallery/projects/darlingperfmon/index.html)

## Dashboards

Dashboards are split into two groups: operational monitoring (PerfMon) and cost/efficiency
analysis (FinOps). Both groups share the `$server` template variable and link to each other.

### PerfMon dashboards

| Dashboard | Description |
|---|---|
| **Fleet Overview** | **Always start here**. Sortable monitored servers, worst appears at the top. Per-server health signals (CPU, threads, memory, blocking, deadlocks, collectors). Click a server to open its Overview. |
| **Overview** | Correlated timeline lanes (CPU, blocking, file I/O) with baseline/anomaly bands, and a rolled-up daily history. |
| **Query Performance** | Query CPU trends, active query snapshots, top queries by CPU/reads, procedure stats, parameter sensitivity, Query Store, long-running queries. |
| **Wait Analysis** | Wait stats by type, latch and spinlock contention. |
| **Storage & tempdb** | File I/O latency and throughput, tempdb space and contention. |
| **Blocking & Deadlocks** | Blocking and deadlock trends, current waits, blocked-process reports, deadlock participants. |
| **CPU, Memory & Sessions** | CPU, memory breakdown, session stats, perfmon counters. |
| **System Events** | Corruption, scheduler issues, severe errors, I/O and memory conditions parsed from `system_health`/default trace events. |
| **Collection Health** | Per-collector status, durations, row counts, error log. |
| **Administration** | Current server configuration, recent configuration changes, running/scheduled SQL Agent jobs. |
| **Availability Groups** | Fleet-wide AG topology: one row per group with primary, replicas, and worst severity. |
| **Query History** _(drill-down)_ | Full collection history for a single query, opened via data link from Query Performance. |
| **Query Store History** _(drill-down)_ | Query Store history for a single query, opened via data link from Query Performance. |
| **Procedure History** _(drill-down)_ | Same as Query History, scoped to a stored procedure. |
| **Wait Drill-Down** _(drill-down)_ | Time-series breakdown for a single wait type, opened via data link from Wait Analysis. |
| **Deadlock Detail** _(drill-down)_ | Participants, victim, and raw XML for a single deadlock event, opened via data link from Blocking & Deadlocks. |
| **Collection Log Detail** _(drill-down)_ | Full collector run log for a server, opened via data link from Collection Health. |
| **Availability Group Detail** _(drill-down)_ | Per-replica and per-database detail for a single AG, opened via data link from Availability Groups. |

### FinOps dashboards

| Dashboard | Description |
|---|---|
| **Recommendations** | Cost-saving recommendations: unused indexes, idle databases, missing indexes, oversized allocations. |
| **Server Inventory** | Cross-server table of properties, edition, version, uptime, health, collected metric counts. |
| **Utilization & Database Resources** | CPU/memory utilization trends, provisioning efficiency, per-database resource usage. |
| **Workload & Contention** | Highest-impact queries by cost, connection patterns by application/login, lock waits and top contended objects. |
| **Capacity & Growth** | Current database/log file sizes and their growth trend over time. |
| **Optimization & Indexing** | Idle databases, tempdb pressure, wait stats summary, missing/duplicate/contended indexes. |
| **Object Sizes & Growth** _(drill-down)_ | Table and index sizes with recent growth, opened via data link from Capacity & Growth. |
| **Index Detail** _(drill-down)_ | Per-index seek/scan/lookup/update counts, opened via data link from Object Sizes & Growth. |

---

## Getting started - pick your path

| | Path |
|---|---|
| You already have a Darling collector and store running | [Just the dashboards](#just-the-dashboards) |
| You want Grafana provisioning automated too (requires [Ansible](https://docs.ansible.com)) | [Complete solution](#complete-solution) |
| You want to try it locally before committing | [Local demo](#local-demo) |

---

## Just the dashboards

Use this path if the Darling collector service and its central store are already running and
collecting, and you have an existing Grafana deployment.

### Prerequisites

- A Darling collector service registered and collecting against at least one SQL Server instance.
- The store's least-privilege Postgres roles provisioned - see
  `Darling/tools/provision-roles.sql` in the upstream project. Grafana connects as the read-only
  `viewer` role.
- Grafana with Unified Alerting enabled (`GF_UNIFIED_ALERTING_ENABLED=true`), if you want alert
  rules too.
- Optional, for Plan XML panels: `plpython3u` and a `public.darling_gunzip(bytea) RETURNS
  text` function on the store (temporary workaround until we have an upstream fix, tracked
  [here](https://github.com/erikdarlingdata/PerformanceMonitor/issues/2071)).

### Step 1: Add the Grafana datasource

Add one **PostgreSQL** datasource pointing at the Darling store.

| Grafana setting | Value |
|---|---|
| Name | any name of your choice - dashboards reference it by UID, not name |
| Host | the store's host and port |
| Database | the Darling database |
| User | `viewer` (or whatever the store's read-only role is named) |
| Password | the `viewer` role's password |
| TLS/SSL mode | match your store's configuration |

Set the datasource's **UID** explicitly (Grafana does not expose this in the UI by default - set it
via provisioning YAML, or note the UID from the URL after creating it through the UI). Every
dashboard JSON file references the UID `darling`; either set your datasource's UID to `darling`, or
re-generate the dashboards with a different UID (see [Dashboard generation](#dashboard-generation)).

Save and **Test** the datasource before importing dashboards.

### Step 2: Import the dashboards

Download the JSON files from
[ansible/roles/perfmon_grafana/files/grafana/dashboards/darling](ansible/roles/perfmon_grafana/files/grafana/dashboards/darling)
and import them in Grafana via **Dashboards - Import**. The dashboards link to each other by UID,
so navigation links will not work unless all are present.

After importing, open any dashboard and select a server from the **$server** dropdown at the top.
If it's empty, the datasource UID doesn't match `darling`, or the store has no rows in
`config.config_monitored_servers`.

### Common pitfalls

| Symptom | Likely cause | Fix |
|---|---|---|
| `$server` dropdown is empty | Datasource UID isn't `darling`, or no servers registered in the store | Fix the datasource UID, or check `config.config_monitored_servers` |
| Panels show "datasource not found" | Datasource UID doesn't match what the dashboard JSON expects | Set the datasource UID to `darling` |
| Panels show "No data" | Collector isn't running, or hasn't collected for the selected server yet | Open **Collection Health**; check for recent successful runs |
| Data looks stale or frozen | Collection stopped for that server | Open **Collection Health**; check collector status per server |

---

## Complete solution

Use this path if you want the Darling collector service configured and Grafana provisioned from
one command. Ansible handles the collector's config file, the store's monitored-server registry,
the Grafana datasource, dashboards, and alert rules.

> [!TIP]
> These are plain, idempotent `ansible-playbook` invocations - point any automation runner
> (AWX/Tower, Jenkins, Rundeck, GitHub Actions, etc.) at the same command for a one-click run
> instead of running it by hand.

### Prerequisites

- Ansible control node with `psycopg2` installed (the `community.postgresql` collection needs it
  for registry reconciliation).
- The Darling collector service already installed on its host - this role configures it, it does
  not install the service binary.
- The store's least-privilege Postgres roles provisioned - see
  `Darling/tools/provision-roles.sql` in the upstream project.
- Optional, for Plan XML panels: `plpython3u` and a `public.darling_gunzip(bytea) RETURNS
  text` function on the store.
- Grafana instance with Unified Alerting enabled.
- `grafana_api_key`: a Grafana service account token with Admin role. Set via vault or group vars.

Install the required Ansible collections once:

```bash
ansible-galaxy collection install -r requirements.yml
```

### Step 1: Edit the inventory

Add your SQL Server instances under `sql_servers` - both roles read this same group. Add a
`darling` group with the host where the collector service runs, and a `grafana` group with your
Grafana host. For example, see [ansible/inventory/hosts.yml](ansible/inventory/hosts.yml):

```yaml
sql_servers:
  hosts:
    sql01:
      ansible_host: sql01.example.com   # what the collector connects to
darling:
  hosts:
    darling-collector:
      ansible_host: darling.example.com
grafana:
  hosts:
    grafana:
      ansible_host: grafana.example.com
      grafana_url: http://grafana.example.com:3000
```

Set credentials in group vars or an Ansible Vault file:

- `perfmon_darling` Ansible role: `perfmon_darling_pg_password` (password for the store's Postgres
  collector role), plus `perfmon_darling_sql_username`/`perfmon_darling_sql_password` for each
  instance using SQL auth.
- `perfmon_grafana` Ansible role: `grafana_api_key` and `perfmon_darling_pg_password` (same
  variable name as above, but here it's the password for the store's Postgres `viewer` role -
  different default from the collector one).

See the [perfmon_darling](ansible/roles/perfmon_darling/README.md) and
[perfmon_grafana](ansible/roles/perfmon_grafana/README.md) role docs for the full variable list.

### Step 2: Deploy

```bash
ansible-playbook -i ansible/inventory/hosts.yml ansible/playbooks/main.yml
```

Or run steps separately:

```bash
ansible-playbook -i ansible/inventory/hosts.yml ansible/playbooks/deploy_perfmon_darling.yml # collector config and registry
ansible-playbook -i ansible/inventory/hosts.yml ansible/playbooks/deploy_perfmon_grafana.yml  # Grafana only
```

What this does:

- Renders the collector's config file and registers new instances in the store's
  `config.config_monitored_servers` registry
- Generates dashboard JSON and imports it into Grafana's Darling folder
- Creates or updates the single Postgres datasource Grafana uses for every server
- Provisions Grafana alert rules per instance (see [Alerting](#alerting))

All steps are safe to re-run. To add an instance later: add it to `hosts.yml` and re-run
`main.yml`.

---

## Local demo

Requires Docker with approximately 6 GB of free RAM.

```bash
cp .env.example .env
docker compose --profile darling up -d
```

What you get:

- Two SQL Server instances (2022 on port 14333, 2025 on port 14334) with active workload
  generators, monitored by a Darling collector writing into a TimescaleDB store
- Grafana at **http://localhost:3000** with all dashboards in the **PerformanceMonitor (Darling)**
  folder

Panels show "datasource not found" until `ansible-runner` completes. Start at **Fleet Overview**.

See [docker/README.md](docker/README.md) for the full service breakdown and troubleshooting.

To stop: `docker compose down`. Add `-v` to also delete data volumes.

---

## Upgrading

`perfmon_darling` role configures an already-installed collector service - renders its config, manages
the store's registry, restarts it on change - but it does not fetch or install the service binary.
Upgrading the collector version itself (a new container image, a new package, however it's
deployed) is a step you take outside this repo; re-run `perfmon_darling` afterward to reconcile its
config and registry against the new version. What this repo's own upgrade path covers is the panel
SQL, if the newer version changed the store's schema:

1. With the stack running, smoke-test every panel query:

   ```bash
   python3 scripts/verify-panels.py darling
   ```

   Any panel whose SQL references a renamed or dropped column fails with a SQL error. Fix the
   column reference in the relevant module under
   `ansible/roles/perfmon_grafana/files/darling_defs/`, then re-run the Grafana playbook to
   regenerate and reimport:

   ```bash
   ansible-playbook -i ansible/inventory/hosts.yml ansible/playbooks/deploy_perfmon_grafana.yml --tags dashboards
   ```

2. Check for new tabs, panels, or schema changes upstream (there is no automated way to detect
   these):

   ```bash
   git -C ../PerformanceMonitor diff <old-tag> <new-tag> -- Darling/PerformanceMonitor.Darling.Viewer/   # new tabs/panels
   git -C ../PerformanceMonitor diff <old-tag> <new-tag> -- Darling/PerformanceMonitor.Darling.Storage/   # schema changes
   ```

---

## How it works

### System overview

Darling has two independent layers: a **collector service** that polls monitored SQL Server
instances and writes into a central PostgreSQL/TimescaleDB store, and a **presentation layer**
built with Grafana that reads that store. Grafana queries Postgres through a single shared
datasource, filtered per dashboard by the server that is selected.

### Dashboard generation

The JSON files in `ansible/roles/perfmon_grafana/files/grafana/dashboards/darling/` are generated
artifacts. The canonical source is
`ansible/roles/perfmon_grafana/files/build-darling-dashboards.py`, which imports per-dashboard
Python modules under `files/darling_defs/`. Every panel, query, variable, row, link, and threshold
is defined in Python and serialized to JSON when you run the builder:

```bash
ansible-playbook -i ansible/inventory/hosts.yml ansible/playbooks/deploy_perfmon_grafana.yml --tags generate
```

The JSON files are committed so Grafana can load them without running the builder. They must not
be hand-edited - the next builder run overwrites them completely. After regenerating, re-run the
role with `--tags dashboards` to push the updates.

`scripts/verify-panels.py` executes every panel's SQL against a live datasource and reports the
result. A SQL error fails; zero rows is not a failure. Run this after modifying panel SQL.

### The `$server` variable and datasource naming

Every dashboard declares a `$server` template variable, populated by a live query against
`config.config_monitored_servers` in the store. Selecting a server filters every panel's query by
that server's `server_id`.

`server_id` is a deterministic hash of the server's storage name (host, plus database for Azure
SQL, plus a read-only-intent suffix), computed identically by the Ansible role and the collector
service so a row either side writes is the row the other reads. See the
[perfmon_darling](ansible/roles/perfmon_darling/README.md) role doc for the full registry model.

The Grafana datasource UID must be `perfmon_darling_ds_uid` (default `darling`); it's baked into
the generated dashboard JSON. Changing it requires regenerating the dashboards with a matching
UID.

### Fleet Overview and severity

The Fleet Overview computes six per-server health signals over a fixed trailing hour, independent
of the dashboard's selected time range: CPU, threads, memory, blocking, deadlocks, and collectors.
Each is scored Healthy, Warning, or Critical by its own thresholds; the worst of the six becomes
that server's overall severity. A freshness check (no collection in the last 2 minutes = Stale,
15 minutes = Offline) can independently push a server to Warning or Offline regardless of its
metric severity. The table sorts worst-first by a composite score (band rank, then per-signal
Critical/Warning counts, then blocking/deadlock incident counts), so the servers needing attention
most rise to the top.

### Ansible roles

**[`perfmon_darling`](ansible/roles/perfmon_darling/README.md)** configures the collector service:
renders its config file from `perfmon_darling_instances` (derived from the `sql_servers`
inventory group), restarts the service on change, and reconciles the store's
`config.config_monitored_servers` registry - inserting newly-inventoried instances, optionally
disabling ones that have left inventory.

**[`perfmon_grafana`](ansible/roles/perfmon_grafana/README.md)** provisions the Grafana side via
the Grafana HTTP API: generates dashboard JSON and imports it into a folder, creates the single
Postgres datasource, and provisions Unified Alerting rule groups (one set per SQL Server
instance), contact points, mute timings, and the notification policy tree - all via the
Provisioning API, no file provisioning.

Both roles are safe to re-run.

### Naming conventions

| Thing | Pattern | Example |
|---|---|---|
| Datasource UID | `perfmon_darling_ds_uid` | `darling` |
| PerfMon dashboard UID | `darling-<slug>` | `darling-blocking-deadlocks` |
| FinOps dashboard UID | `darling-finops-<slug>` | `darling-finops-utilization` |
| Grafana folder | `grafana_darling_folder_uid` | `perfmon-darling` |

### Role documentation

- [ansible/roles/perfmon_darling/README.md](ansible/roles/perfmon_darling/README.md) - full
  variable reference, per-instance connection settings, registry reconciliation model
- [ansible/roles/perfmon_grafana/README.md](ansible/roles/perfmon_grafana/README.md) - full
  variable reference, alert threshold variables, teardown

---

## Alerting

Grafana Unified Alerting is provisioned with alert rules ported from upstream's
`DarlingAlertSettings.cs` / `AlertEngine.cs`, plus the Availability Group rules ported from
`AgAlertPolicy.cs`. Rules evaluate every minute against the Darling store, filtered per instance
by `server_id`.

### Alert rules

| Alert | Default threshold |
|---|---|
| High CPU | latest collected CPU sample >= 80% |
| Blocking Detected | captured blocking events in the last hour >= 1 |
| Deadlocks Detected | deadlock count in the last 5 minutes >= 1 |
| TempDB Space | latest used >= 80% of allocated space |
| Low Disk Space | latest free < 10% OR < 5 GB on any volume |
| Long-Running Query | any query currently running >= 30 min |
| Poison Wait | avg ms per wait event >= 500 ms for `THREADPOOL`, `RESOURCE_SEMAPHORE`, or `RESOURCE_SEMAPHORE_QUERY_COMPILE` |
| Long-Running Job | current SQL Agent job run >= 3x its average duration |
| Failed Job | most recent SQL Agent job run was a failure |
| Collection Stopped | no collector has logged a run for an instance in 30 minutes |
| AG Failover | an Availability Group replica's role changed since its prior reading |
| AG Replica Disconnected | a replica's latest `connected_state_desc` is `DISCONNECTED` |
| AG Database Suspended | a database's latest `is_suspended` reading is true |
| AG Sync Fell Behind | a database's latest lag >= 300s (redo queue check off by default) |

All thresholds are Ansible variables defined in `roles/perfmon_grafana/defaults/main.yml`. Override per-host in `host_vars/` or per-group in `group_vars/`.

### Default behavior: silent

Alerts fire and are tracked in Grafana but no notifications are sent until a contact point is
configured. Evaluation runs and state is visible in the Grafana Alerts UI regardless.

### Enabling delivery

Set `perfmon_alert_contact_points` in your inventory and re-run the `perfmon_grafana` role. Each
entry is a contact point object passed to the Grafana API. All entries must share the same `name`
value - Grafana treats them as one receiver with multiple integrations.

```yaml
# e.g. in ansible/inventory/group_vars/grafana.yml
perfmon_alert_contact_points:
  - uid: perfmon-slack
    name: perfmon-alerts
    type: slack
    settings:
      url: "https://hooks.slack.com/services/T000000/B000000/XXXXXXXXXXXXXXXXXXXXXXXX"
      recipient: "#alerts"
      title: "PerfMon Alert"
  - uid: perfmon-pagerduty
    name: perfmon-alerts
    type: pagerduty
    settings:
      integrationKey: abc123def456abc123def456abc123de
```

The notification policy route targets `perfmon_alert_receiver_name` (default `perfmon-alerts`).
Add multiple entries to fire more than one integration per alert. Email requires `GF_SMTP_*` env
vars on the Grafana server; set `type: email` and `settings.addresses` for it.

### Unreachable data

If a collector's connection to its monitored server queues behind application workload, that
server's data in the store falls behind. Watch **Collection Health** or the Collection Stopped
alert for this. If the store itself becomes unreachable, every alert rule and every dashboard
panel fails together, since all of them read through the single shared datasource.

---

## Known limitations

### Features with no Grafana equivalent

| Feature | Why |
|---|---|
| **Live active-query snapshot** | Needs a live DMV connection to the monitored server; Grafana here talks only to Postgres. |
| **Graphical query plan / deadlock graph viewer** | Grafana has no built-in ShowPlan/XDL renderer. Affected panels show the XML text for copy-paste into SSMS or Erik's standalone viewer. |
| **Index Analysis consolidation engine** | Duplicate/subset/superset index detection and generated DDL scripts are not re-derived as panel SQL. The Optimization & Indexing dashboard covers the analyzer's snapshot-derived half only; use the upstream Viewer's FinOps -> Index Analysis tab for the rest. |
| **Wait Drill-Down: Correlated/Uncapturable/Chain paths** | Only the Filtered path is ported. |
| **Collector "Purge Now"** | Sends a live control command to the service; Grafana only reads the store. |
| **MCP server** | This project does not include an MCP service. Grafana's own mcp-grafana project provides MCP access to Grafana itself, but a separate service querying the store directly would be needed to expose the monitoring data. |
| **Side-by-side query comparison** | Compare a query's performance across two separate time ranges. Not currently supported. |

### Timezone

Timestamps in the store are naive UTC. Grafana's time-range filters and time axes work directly
against them with no offset correction needed. The one exception:
`cpu_utilization_stats.sample_time` is server-local at the source and corrected to UTC in SQL
before being returned, matching upstream's own handling.

---

## License

[MIT License](LICENSE).

### Dependency licenses

| Dependency | License |
|---|---|
| [Erik Darling's PerformanceMonitor](https://github.com/erikdarlingdata/PerformanceMonitor) | MIT |

> [!NOTE]
> The Darling collector service is not bundled in this repository - it's deployed independently,
> wherever you choose to run it. The Grafana panel SQL in
> [`darling_defs/`](ansible/roles/perfmon_grafana/files/darling_defs/) is bundled in this
> repository: query logic (column expressions, aggregation, CASE branches) is copied from
> PerformanceMonitor's C# Darling Viewer queries and reworked to run through Grafana's macros,
> permitted under PerformanceMonitor's MIT license.
