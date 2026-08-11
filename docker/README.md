# Docker environment

Self-contained demo stack: SQL Server instances with workload generators, a central Darling
store, and an Ansible runner that provisions Grafana automatically.

See `docker-compose.yml` at the repo root for the full service definition.

## Services

| Service | Container | Port (host) | Purpose |
|---|---|---|---|
| `mssql-2022` | `perfmon-mssql-2022` | 14333 | SQL Server 2022 - primary workload instance |
| `mssql-2025` | `perfmon-mssql-2025` | 14334 | SQL Server 2025 - memory pressure instance |
| `workload` | `perfmon-workload` | - | Generates realistic query load against `mssql-2022` |
| `workload-memory` | `perfmon-workload-memory` | - | Memory-pressure workload against `mssql-2025` |
| `grafana` | `perfmon-grafana` | 3000 | Grafana UI with provisioned dashboards |
| `ansible-runner` | `perfmon-ansible-runner` | - | Runs the Ansible playbook that configures Grafana |
| `darling-pg` (profile `darling`) | `perfmon-darling-pg` | - | Central TimescaleDB store the collector writes into |
| `darling` (profile `darling`) | `perfmon-darling` | - | The Darling collector service, monitoring both SQL Server instances |
| `darling-provision` (profile `darling`) | `perfmon-darling-provision` | - | One-shot: waits for the store to migrate, then provisions the `darling`/`viewer` roles |
| `darling-collector-config` (profile `darling`) | `perfmon-darling-collector-config` | - | One-shot: waits for the store to migrate, then enables collectors that default off fleet-wide |

All SQL Server instances run as Developer Edition with SQL Agent enabled.

## Prerequisites

- Docker with Compose v2
- ~6 GB free RAM, approximately 2 GB per SQL Server instance
- Ports 14333, 14334, and 3000 free on the host

## Quick start

```bash
cp .env.example .env
# Edit .env - set MSSQL_SA_PASSWORD, GRAFANA_ADMIN_PASSWORD, DARLING_PG_PASSWORD, DARLING_VIEWER_PASSWORD
docker compose --profile darling up -d
```

`ansible-runner` waits for the SQL Server instances, Grafana, and the Darling store to all be
healthy, then runs the full Ansible playbook (`ansible/playbooks/main.yml`), configuring the
Darling collector role and provisioning Grafana's datasource, dashboards, and alert rules. The
`darling` container waits in a poll loop until this run renders its config onto the shared
`darling-config` volume, then starts collecting; `darling-provision` and `darling-collector-config`
likewise wait for `darling` to migrate the store before running their own SQL.

```bash
docker compose logs -f ansible-runner # watch provisioning progress
```

Grafana starts at **http://localhost:3000**. Panels show "datasource not found" until
`ansible-runner` completes. Start at **Fleet Overview** once it exits.

## Environment variables

Defined in `.env`:

| Variable | Purpose |
|---|---|
| `MSSQL_SA_PASSWORD` | SA password for both SQL Server instances |
| `GRAFANA_ADMIN_PASSWORD` | Grafana admin password |
| `GRAFANA_API_KEY` | Populated automatically by `bootstrap/setup-grafana-api-key.sh` on each `ansible-runner` run |
| `DARLING_PG_PASSWORD` | Password for the store's `darling` (owner/collector) Postgres role |
| `DARLING_VIEWER_PASSWORD` | Password for the store's `viewer` (read-only) Postgres role, used by Grafana's datasource |

The SA password must meet SQL Server's complexity requirements (uppercase, lowercase, digit,
symbol; minimum 8 characters).

## SQL Server version notes

Both instances use `mssql-tools18` at `/opt/mssql-tools18/bin/sqlcmd` for their healthchecks and
connect with TLS using `tlsSkipVerify`. The Darling collector and the workload generators are the
only things that connect to them - there is no Ansible role installing anything onto SQL Server
itself.

## Darling store

`darling-pg` is a TimescaleDB container the `darling` service migrates on first start. The
`darling` container builds from `docker/darling/Dockerfile` (which fetches the pinned
`PERFMON_VERSION` collector release) and waits on `docker/darling/entrypoint.sh` for
`darling.json` to appear on the shared `darling-config` volume - see
[Ansible inventory](#ansible-inventory) below for where that file comes from.

`darling-provision` and `darling-collector-config` are one-shot containers (`restart: "no"`) that
each poll until the service has migrated the schema they depend on, then run their SQL and exit:

- `darling-provision` runs upstream's `provision-roles.sql` (fetched from the pinned
  `PERFMON_VERSION` tag) to create the least-privilege `darling`/`viewer` Postgres roles. A real
  store is expected to already have these roles provisioned by its operator; the Ansible role
  never does this itself.
- `darling-collector-config` enables collectors that default off fleet-wide with no
  `config.config_collector_schedules` row (currently `long_query_completions`).

## Workload generator

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

## Re-running Ansible

To re-provision after changing Ansible roles or inventory; for example, after modifying alerting
variables:

```bash
docker compose run --rm ansible-runner
```

`ansible-runner` has `restart: "no"`, so its container exits after each run; `docker compose run`
starts a fresh one.

## Stopping and cleanup

```bash
docker compose down # stop containers, but keep volumes
docker compose down -v # stop containers and delete all data volumes
```

## Connecting directly

From the host (using `sqlcmd` from mssql-tools18):

```bash
sqlcmd -S localhost,14333 -U sa -P "$MSSQL_SA_PASSWORD" -C # 2022
sqlcmd -S localhost,14334 -U sa -P "$MSSQL_SA_PASSWORD" -C # 2025
```

From inside the stack, use the container hostnames (`mssql-2022`, `mssql-2025`) on port 1433.

The Darling store is not published to the host; connect from inside the stack:

```bash
psql -h darling-pg -U darling -d darling
```
or via docker:

```bash
docker compose exec darling-pg psql -U darling -d darling
```

## Ansible inventory

The docker-internal inventory lives at `ansible/inventory/docker/`, used only by the
`ansible-runner` container - it is not intended for use from the host. The host-facing inventory
is `ansible/inventory/`.

The `darling` group has a single `ansible_connection: local` host: running the `perfmon_darling`
role against it renders `darling.json` onto the `darling-config` volume the `darling` container
reads from, and reconciles `config.config_monitored_servers` in the real `darling-pg` store - the
`sql_servers` group is the one source of truth for which instances get monitored.

## Smoke-testing panels

After provisioning completes, run the panel smoke test against the Darling datasource:

```bash
GRAFANA_API_KEY=$(grep '^GRAFANA_API_KEY=' .env | cut -d= -f2-) python3 scripts/verify-panels.py darling
```

A SQL error prints `FAIL` and causes a non-zero exit. Zero rows is not a failure.
