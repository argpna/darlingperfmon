# Docker environment

`docker/compose.darling.yml` defines the Darling store and collector as a standalone, reusable
service stack. `docker-compose.yml` at the repo root includes the darling compose file and builds a
self-contained demo stack with mssql workload generators and an Ansible runner that provisions
Grafana.

## Darling service stack

| Service | Container | Purpose |
|---|---|---|
| `darling-pg` (profile `darling`) | `perfmon-darling-pg` | Central TimescaleDB store |
| `darling` (profile `darling`) | `perfmon-darling` | The Darling collector service, monitoring SQL Server instances and writes to the TimescaleDB |
| `darling-provision` (profile `darling`) | `perfmon-darling-provision` | One-shot: waits for the store to migrate, then provisions the `darling`/`viewer` roles |
| `darling-collector-config` (profile `darling`) | `perfmon-darling-collector-config` | One-shot: waits for the store to migrate, then enables collectors that default off fleet-wide |
| `darling-plan-gunzip` (profile `darling`) | `perfmon-darling-plan-gunzip` | One-shot: enables `plpython3u` and the `darling_gunzip()` UDF Plan XML panels fall back to |

Environment variables, defined in `.env`:

| Variable | Purpose |
|---|---|
| `DARLING_PG_PASSWORD` | Password for the store's `darling` (owner/collector) Postgres role |
| `DARLING_VIEWER_PASSWORD` | Password for the store's `viewer` (read-only) Postgres role, used by Grafana's datasource |
| `PERFMON_VERSION` | Collector release tag `darling` builds from and `darling-provision` fetches `provision-roles.sql` from. Defaults to `v3.4.0` |

`darling-pg` is a TimescaleDB container the `darling` service migrates on first start. The
`darling` container builds from `docker/darling/Dockerfile` and waits on
`docker/darling/entrypoint.sh` for `darling.json` to appear on the shared `darling-config` volume.
`entrypoint.sh` keeps watching the file afterward and restarts the collector process whenever it
changes, so re-running Ansible after adding an instance takes effect with no manual restart.

`darling-provision` and `darling-collector-config` are one-shot containers (`restart: "no"`) that
each poll until the service has migrated the schema they depend on, then run their SQL and exit:

- `darling-provision` runs upstream's `provision-roles.sql` (fetched from the pinned
  `PERFMON_VERSION` tag) to create the least-privilege `darling`/`viewer` Postgres roles. A real
  store is expected to already have these roles provisioned by its operator; the Ansible role
  never does this itself.
- `darling-collector-config` enables collectors that default off fleet-wide with no
  `config.config_collector_schedules` row (currently `long_query_completions`).

`darling-plan-gunzip` only needs `darling-pg` itself healthy, not the collector's schema, so it has
no such polling step.

### Standalone Darling stack

Run `docker/compose.darling.yml` directly to deploy just the collector and store (without the
demo):

```bash
# docker/.env
DARLING_PG_PASSWORD=...
DARLING_VIEWER_PASSWORD=...

docker compose -f docker/compose.darling.yml --profile darling up -d
```

Ansible still has to render `darling.json` and reconcile the store's registry; run
`deploy_perfmon_darling.yml` directly against your own inventory instead, pointing it at wherever
the `darling` container can read the rendered config from. The simplest option is to bind-mount a
host directory in place of the `darling-config` volume (override `darling`'s `/etc/darling` mount
in a local compose override file) and set `perfmon_darling_config_path` to the matching host path
in your inventory:

```bash
ansible-playbook -i <your inventory> ansible/playbooks/deploy_perfmon_darling.yml
```

`perfmon_darling_pg_host`/`perfmon_darling_pg_port` goes into both `darling.json`'s connection
string (resolved by the `darling` container, on the compose network) and the Ansible role's
registry-reconciliation tasks (`delegate_to: localhost` - resolved from wherever `ansible-playbook`
runs). If Ansible runs from outside the compose network, `darling-pg`'s docker-internal
DNS-name may not resolve properly; publish its port and make the same host:port reachable from both
sides (either a `/etc/hosts` entry pointing `darling-pg` at the published port, or a real DNS record).

The store is not published to the host; connect from inside the stack:

```bash
psql -h darling-pg -U darling -d darling
```
or via docker:

```bash
docker compose -f docker/compose.darling.yml exec darling-pg psql -U darling -d darling
```

To stop:

```bash
docker compose -f docker/compose.darling.yml down # stop containers, but keep volumes
docker compose -f docker/compose.darling.yml down -v # stop containers and delete all data volumes
```

## Demo stack

Builds on the Darling service stack above by adding SQL Server instances with active workload,
Grafana, and an Ansible runner - the whole system comes up with one command.

| Service | Container | Port (host) | Purpose |
|---|---|---|---|
| `mssql-2022` | `perfmon-mssql-2022` | 14333 | SQL Server 2022 - primary workload instance |
| `mssql-2025` | `perfmon-mssql-2025` | 14334 | SQL Server 2025 - memory pressure instance |
| `workload` | `perfmon-workload` | - | Generates realistic query load against `mssql-2022` |
| `workload-memory` | `perfmon-workload-memory` | - | Memory-pressure workload against `mssql-2025` |
| `grafana` | `perfmon-grafana` | 3000 | Grafana UI with provisioned dashboards |
| `ansible-runner` | `perfmon-ansible-runner` | - | Runs the Ansible playbook that configures Grafana |

All SQL Server instances run as Developer Edition with SQL Agent enabled.

### Prerequisites

- Docker with Compose v2
- ~6 GB free RAM, approximately 2 GB per SQL Server instance
- Ports 14333, 14334, and 3000 free on the host

### Quickstart

```bash
cp .env.example .env
# Edit .env - set MSSQL_SA_PASSWORD, GRAFANA_ADMIN_PASSWORD, DARLING_PG_PASSWORD, DARLING_VIEWER_PASSWORD
docker compose --profile darling up -d
```

`ansible-runner` waits for the SQL Server instances, Grafana, and the Darling store to all be
healthy, then runs the full Ansible playbook (`ansible/playbooks/main.yml`), configuring the
Darling collector role and provisioning Grafana's datasource, dashboards, and alert rules. The
`darling` container waits in a poll loop until the config is rendered onto the shared
`darling-config` volume, then starts collecting data; `darling-provision` and `darling-collector-config`
likewise wait for `darling` to migrate the store before running their own SQL.

```bash
docker compose logs -f ansible-runner # watch provisioning progress
```

Grafana starts at **http://localhost:3000**. Panels may show "datasource not found" until
`ansible-runner` completes. Start at **Fleet Overview** once the runner exits.

Additional environment variables, defined in the same `.env`:

| Variable | Purpose |
|---|---|
| `MSSQL_SA_PASSWORD` | SA password for both SQL Server instances |
| `GRAFANA_ADMIN_PASSWORD` | Grafana admin password |
| `GRAFANA_API_KEY` | Populated automatically by `bootstrap/setup-grafana-api-key.sh` on each `ansible-runner` run |

### Workload generator

The `workload` container runs `scripts/workload.sh` against `mssql-2022` in a loop. Each cycle:

- Stored procedure calls with alternating parameters
- Ad-hoc query bursts
- DDL events
- Single-use plan generation then periodic `DBCC FREEPROCCACHE`
- Blocking pair (holder waits 40s, victim times out)
- Deadlock attempt every third cycle

The `workload-memory` container runs `scripts/workload-memory.sh` against `mssql-2025`, generating
memory-pressure workload, `RESOURCE_SEMAPHORE` waits, memory grant queue activity. Both instances
have active workload so the Darling store has data to display.

### Re-running Ansible

To re-provision after changing Ansible roles or inventory, run:

```bash
docker compose run --rm ansible-runner
```

### Ansible inventory

The docker-internal inventory lives at `ansible/inventory/docker/`, used only by the
`ansible-runner` container - it is not intended for use from the host. The host-facing inventory
is `ansible/inventory/`.

The `darling` group has a single `ansible_connection: local` host: running the `perfmon_darling`
role against it renders `darling.json` onto the `darling-config` volume the `darling` container
reads from, and reconciles `config.config_monitored_servers` in the real `darling-pg` store - the
`sql_servers` group is the one source of truth for which instances get monitored.

### Connecting directly

From the host:

```bash
sqlcmd -S localhost,14333 -U sa -P "$MSSQL_SA_PASSWORD" -C # 2022
sqlcmd -S localhost,14334 -U sa -P "$MSSQL_SA_PASSWORD" -C # 2025
```

From inside the stack, use the container hostnames (`mssql-2022`, `mssql-2025`) on port 1433.

### Smoke-testing panels

After provisioning completes, run the panel smoke test against the Darling datasource:

```bash
GRAFANA_API_KEY=$(grep '^GRAFANA_API_KEY=' .env | cut -d= -f2-) python3 scripts/verify-panels.py darling
```

### Stopping and cleanup

```bash
docker compose down # stop containers, but keep volumes
docker compose down -v # stop containers and delete all data volumes
```
