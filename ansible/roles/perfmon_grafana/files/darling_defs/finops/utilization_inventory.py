"""FinOps Utilization & Inventory dashboard (Darling line) - merges Utilization,
Server Inventory, and Database Resources dashboards into one compute/memory/IO/cost view.

Upstream ref: ViewerDataService.FinOps.Utilization.cs, .Workload.cs, .Inventory.cs.

Upstream's progress-bar strip becomes a table with gauge cells. Server Inventory carries no
$server filter - upstream's query is cross-server by design.
"""

from .._shared import (
    CAGG_TIME_COL,
    SERVER_REGISTRY,
    UTC_NOW,
    col_gauge_bar,
    col_thresholds,
    col_unit,
    collector,
    finops_dashboard,
    n0,
    reset_id,
    rollup,
    server_filter,
    server_join,
    server_var,
    status_colors,
    subtab,
    table,
    tiered,
    uid,
)
from ._shared import (
    HEALTH_SCORE_STEPS,
    IDLE_DAYS,
    IDLE_DB_EXCLUSIONS,
    PROVISIONING_STATUS_COLORS,
    TREND_DAYS,
    budget_cte,
    classification_explanation,
    cpu_score,
    latest_per_server,
    memory_score,
    overall_score,
    provisioning_status,
    status_display,
    storage_score,
)

_CPU = collector("cpu_utilization_stats")
_MEM = collector("memory_stats")
_PROPS = collector("server_properties")
_SIZES = collector("database_size_stats")
_QUERY_STATS = collector("query_stats")
_FILE_IO = collector("file_io_stats")

# GetUtilizationEfficiencyAsync's cutoff. Always inside the raw horizon, so no tiered() here.
_LAST_24H = f"{UTC_NOW} - INTERVAL '24 hours'"

_STOLEN_PCT = """CASE WHEN m.total_server_memory_mb > 0
        THEN (m.total_server_memory_mb - m.buffer_pool_mb)::numeric * 100.0
             / m.total_server_memory_mb
        ELSE 0 END"""

_BP_PCT = """CASE WHEN m.total_physical_memory_mb > 0
        THEN m.buffer_pool_mb::numeric * 100.0 / m.total_physical_memory_mb
        ELSE 0 END"""

# Storage half of the health score (LoadFinOpsUtilizationAsync). A file with no used_size_mb
# contributes no free space, and a server with no sizes at all scores as fully free.
_FREE_PCT = """CASE WHEN COALESCE(st.total_mb, 0) > 0
        THEN st.free_mb * 100.0 / st.total_mb
        ELSE 100 END"""

# Shared by the Utilization Efficiency and Server Inventory queries below - both alias their
# per-server CPU CTE `c` and memory CTE `m`, so the same status expression fits either.
_STATUS = provisioning_status(
    "c.avg_cpu_pct", "c.max_cpu_pct", "c.p95_cpu_pct", "m.memory_ratio"
)

_EFFICIENCY_SQL = f"""
WITH cpu_stats AS (
    SELECT server_id,
           AVG(sqlserver_cpu_utilization::numeric) AS avg_cpu_pct,
           MAX(sqlserver_cpu_utilization) AS max_cpu_pct,
           PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY sqlserver_cpu_utilization)::numeric
               AS p95_cpu_pct,
           COUNT(*) AS cpu_samples
    FROM {_CPU}
    WHERE {server_filter()} AND collection_time >= {_LAST_24H}
    GROUP BY server_id
),
mem_latest AS (
    SELECT DISTINCT ON (server_id)
           server_id,
           total_server_memory_mb,
           target_server_memory_mb,
           total_physical_memory_mb,
           buffer_pool_mb,
           max_workers_count,
           current_workers_count,
           total_server_memory_mb::numeric / NULLIF(target_server_memory_mb, 0)
               AS memory_ratio
    FROM {_MEM}
    WHERE {server_filter()}
    ORDER BY server_id, collection_time DESC
),
server_info AS (
    /* vcore_count is the licensable number where the platform bills by vCore. */
    SELECT DISTINCT ON (server_id)
           server_id, COALESCE(vcore_count, cpu_count) AS cpu_count
    FROM {_PROPS}
    WHERE {server_filter()}
    ORDER BY server_id, collection_time DESC
),
storage AS (
    SELECT server_id,
           SUM(total_size_mb) AS total_mb,
           SUM(CASE WHEN used_size_mb IS NOT NULL
                    THEN total_size_mb - used_size_mb ELSE 0 END) AS free_mb
    FROM {_SIZES}
    WHERE {server_filter()} AND {latest_per_server(_SIZES)}
    GROUP BY server_id
),
{budget_cte()}
SELECT
    srv.name AS "Server",
    {status_display(_STATUS)} AS "Status",
    {overall_score(
        cpu_score('c.p95_cpu_pct'),
        memory_score(
            'CASE WHEN m.total_physical_memory_mb > 0'
            ' THEN m.buffer_pool_mb::numeric / m.total_physical_memory_mb ELSE 0 END'
        ),
        storage_score(_FREE_PCT),
    )} AS "Health",
    {classification_explanation(
        _STATUS,
        'c.avg_cpu_pct',
        'c.max_cpu_pct',
        'c.p95_cpu_pct',
        _BP_PCT,
        'COALESCE(m.memory_ratio, 0)',
    )} AS "Why",
    round(c.avg_cpu_pct, 2) AS "Avg CPU %",
    round(c.p95_cpu_pct, 2) AS "P95 CPU %",
    c.max_cpu_pct AS "Max CPU %",
    s.cpu_count AS "CPUs",
    {n0('m.current_workers_count')} || ' / ' || {n0('m.max_workers_count')} AS "Workers",
    c.cpu_samples AS "Samples (24h)",
    round({_STOLEN_PCT}, 1) AS "Stolen Mem %",
    round({_BP_PCT}, 1) AS "Buffer Pool %",
    round(m.memory_ratio, 2) AS "Mem Ratio",
    m.total_physical_memory_mb AS "Physical MB",
    m.target_server_memory_mb AS "Target MB",
    m.total_server_memory_mb AS "Total MB",
    m.buffer_pool_mb AS "Buffer Pool MB",
    round({_FREE_PCT}, 1) AS "Storage Free %",
    b.monthly_cost AS "Budget $/mo",
    b.monthly_cost * 12 AS "Budget $/yr"
FROM cpu_stats c
JOIN mem_latest m ON m.server_id = c.server_id
LEFT JOIN server_info s ON s.server_id = c.server_id
LEFT JOIN storage st ON st.server_id = c.server_id
LEFT JOIN budget b ON b.server_id = c.server_id
{server_join('c.server_id')}
ORDER BY srv.name
"""

# GetProvisioningTrendAsync - a fixed 7 days, classified per day by the same thresholds.
_TREND_STATUS = provisioning_status(
    "c.avg_cpu_pct", "c.max_cpu_pct", "c.p95_cpu_pct", "m.avg_memory_ratio"
)

_TREND_SQL = f"""
WITH daily_cpu AS (
    SELECT server_id,
           collection_time::date AS day,
           AVG(sqlserver_cpu_utilization::numeric) AS avg_cpu_pct,
           MAX(sqlserver_cpu_utilization) AS max_cpu_pct,
           PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY sqlserver_cpu_utilization)::numeric
               AS p95_cpu_pct
    FROM {_CPU}
    WHERE {server_filter()}
      AND collection_time >= {UTC_NOW} - INTERVAL '{TREND_DAYS} days'
    GROUP BY server_id, collection_time::date
),
daily_mem AS (
    SELECT server_id,
           collection_time::date AS day,
           AVG(total_server_memory_mb::numeric / NULLIF(target_server_memory_mb, 0))
               AS avg_memory_ratio
    FROM {_MEM}
    WHERE {server_filter()}
      AND collection_time >= {UTC_NOW} - INTERVAL '{TREND_DAYS} days'
    GROUP BY server_id, collection_time::date
)
SELECT
    c.day AS "Day",
    srv.name AS "Server",
    {status_display(_TREND_STATUS)} AS "Status",
    round(c.avg_cpu_pct, 1) AS "Avg CPU %",
    round(c.p95_cpu_pct, 1) AS "P95 CPU %",
    c.max_cpu_pct AS "Max CPU %",
    round(COALESCE(m.avg_memory_ratio, 0), 2) AS "Mem Ratio"
FROM daily_cpu c
LEFT JOIN daily_mem m ON m.server_id = c.server_id AND m.day = c.day
{server_join('c.server_id')}
ORDER BY c.day DESC, srv.name
"""

# TopResourceConsumersBy*Sql' topN, applied as a per-server rank.
_TOP_N = 5

_CONSUMER_CTES = f"""
workload AS (
    SELECT server_id, database_name,
           SUM(delta_worker_time) / 1000.0 AS cpu_time_ms,
           SUM(delta_execution_count) AS execution_count
    FROM {_QUERY_STATS}
    WHERE {server_filter()}
      AND collection_time >= {_LAST_24H}
      AND delta_worker_time IS NOT NULL
    GROUP BY server_id, database_name
),
io AS (
    SELECT server_id, database_name,
           SUM(delta_read_bytes + delta_write_bytes) / 1048576.0 AS io_total_mb
    FROM {_FILE_IO}
    WHERE {server_filter()}
      AND collection_time >= {_LAST_24H}
      AND delta_read_bytes IS NOT NULL
    GROUP BY server_id, database_name
)"""

_TOP_TOTAL_SQL = f"""
WITH {_CONSUMER_CTES},
combined AS (
    SELECT COALESCE(w.server_id, i.server_id) AS server_id,
           COALESCE(w.database_name, i.database_name) AS database_name,
           COALESCE(w.cpu_time_ms, 0) AS cpu_time_ms,
           COALESCE(w.execution_count, 0) AS execution_count,
           COALESCE(i.io_total_mb, 0) AS io_total_mb
    FROM workload w
    FULL JOIN io i ON i.server_id = w.server_id AND i.database_name = w.database_name
),
totals AS (
    SELECT server_id,
           NULLIF(SUM(cpu_time_ms), 0) AS total_cpu,
           NULLIF(SUM(io_total_mb), 0) AS total_io
    FROM combined
    GROUP BY server_id
),
ranked AS (
    SELECT c.*,
           ROW_NUMBER() OVER (PARTITION BY c.server_id ORDER BY c.cpu_time_ms DESC) AS rn
    FROM combined c
    WHERE c.database_name IS NOT NULL
)
SELECT
    srv.name AS "Server",
    r.database_name AS "Database",
    round(r.cpu_time_ms * 100.0 / t.total_cpu, 1) AS "CPU %",
    round(r.io_total_mb * 100.0 / t.total_io, 1) AS "IO %",
    round(r.cpu_time_ms, 0) AS "CPU (ms)",
    round(r.io_total_mb, 2) AS "IO (MB)",
    r.execution_count AS "Execs"
FROM ranked r
JOIN totals t ON t.server_id = r.server_id
{server_join('r.server_id')}
WHERE r.rn <= {_TOP_N}
ORDER BY srv.name, r.cpu_time_ms DESC
"""

_TOP_AVG_SQL = f"""
WITH {_CONSUMER_CTES},
per_exec AS (
    SELECT w.server_id, w.database_name, w.cpu_time_ms, w.execution_count,
           COALESCE(i.io_total_mb, 0) AS io_total_mb,
           w.cpu_time_ms / w.execution_count AS avg_cpu_ms,
           COALESCE(i.io_total_mb, 0) / w.execution_count AS avg_io_mb
    FROM workload w
    LEFT JOIN io i ON i.server_id = w.server_id AND i.database_name = w.database_name
    WHERE w.execution_count > 0
),
ranked AS (
    SELECT p.*,
           ROW_NUMBER() OVER (PARTITION BY p.server_id ORDER BY p.avg_cpu_ms DESC) AS rn
    FROM per_exec p
)
SELECT
    srv.name AS "Server",
    r.database_name AS "Database",
    round(r.avg_cpu_ms, 2) AS "Avg CPU (ms)",
    round(r.avg_io_mb, 4) AS "Avg IO (MB)",
    r.execution_count AS "Execs",
    round(r.cpu_time_ms, 0) AS "Total CPU (ms)",
    round(r.io_total_mb, 2) AS "Total IO (MB)"
FROM ranked r
{server_join('r.server_id')}
WHERE r.rn <= {_TOP_N}
ORDER BY srv.name, r.avg_cpu_ms DESC
"""

# DatabaseSizeSummarySql, topN 10 per server.
_SIZE_TOP_N = 10

_DB_SIZE_SQL = f"""
WITH sizes AS (
    SELECT server_id, database_name,
           SUM(total_size_mb) AS total_mb,
           SUM(used_size_mb) AS used_mb
    FROM {_SIZES}
    WHERE {server_filter()} AND {latest_per_server(_SIZES)}
    GROUP BY server_id, database_name
),
ranked AS (
    SELECT s.*,
           ROW_NUMBER() OVER (PARTITION BY s.server_id ORDER BY s.total_mb DESC) AS rn
    FROM sizes s
)
SELECT
    srv.name AS "Server",
    r.database_name AS "Database",
    round(r.used_mb, 0) AS "Used MB",
    round(r.total_mb, 0) AS "Allocated MB",
    round(r.used_mb * 100.0 / NULLIF(r.total_mb, 0), 1) AS "Used %"
FROM ranked r
{server_join('r.server_id')}
WHERE r.rn <= {_SIZE_TOP_N}
ORDER BY srv.name, r.total_mb DESC
"""


def _resource_workload_cte(relation: str, time_col: str) -> str:
    """The database-grain CPU/read/write CTE for one retention tier.

    The delta filter is raw-only: the rollup already applied it when materializing.
    """
    if time_col == CAGG_TIME_COL:
        return f"""
    SELECT server_id, database_name,
           SUM(worker_time_sum) / 1000.0 AS cpu_time_ms,
           SUM(logical_reads_sum) AS logical_reads,
           SUM(physical_reads_sum) AS physical_reads,
           SUM(logical_writes_sum) AS logical_writes,
           SUM(execution_count_sum) AS execution_count
    FROM {relation}
    WHERE {server_filter()} AND $__timeFilter({time_col})
    GROUP BY server_id, database_name
"""
    return f"""
    SELECT server_id, database_name,
           SUM(delta_worker_time) / 1000.0 AS cpu_time_ms,
           SUM(delta_logical_reads) AS logical_reads,
           SUM(delta_physical_reads) AS physical_reads,
           SUM(delta_logical_writes) AS logical_writes,
           SUM(delta_execution_count) AS execution_count
    FROM {relation}
    WHERE {server_filter()}
      AND $__timeFilter({time_col})
      AND delta_worker_time IS NOT NULL
    GROUP BY server_id, database_name
"""


def _resource_usage_sql(workload_cte: str) -> str:
    """DatabaseResourceUsageSql for one tier. Percentage denominators are per server."""
    return f"""
WITH workload AS ({workload_cte}),
io AS (
    SELECT server_id, database_name,
           SUM(delta_read_bytes) / 1048576.0 AS io_read_mb,
           SUM(delta_write_bytes) / 1048576.0 AS io_write_mb,
           SUM(delta_stall_read_ms + delta_stall_write_ms) AS io_stall_ms
    FROM {_FILE_IO}
    WHERE {server_filter()}
      AND $__timeFilter(collection_time)
      AND delta_read_bytes IS NOT NULL
    GROUP BY server_id, database_name
),
combined AS (
    SELECT COALESCE(w.server_id, i.server_id) AS server_id,
           COALESCE(w.database_name, i.database_name) AS database_name,
           COALESCE(w.cpu_time_ms, 0) AS cpu_time_ms,
           COALESCE(w.logical_reads, 0) AS logical_reads,
           COALESCE(w.physical_reads, 0) AS physical_reads,
           COALESCE(w.logical_writes, 0) AS logical_writes,
           COALESCE(w.execution_count, 0) AS execution_count,
           COALESCE(i.io_read_mb, 0) AS io_read_mb,
           COALESCE(i.io_write_mb, 0) AS io_write_mb,
           COALESCE(i.io_stall_ms, 0) AS io_stall_ms
    FROM workload w
    FULL JOIN io i ON i.server_id = w.server_id AND i.database_name = w.database_name
),
totals AS (
    SELECT server_id,
           NULLIF(SUM(cpu_time_ms), 0) AS total_cpu,
           NULLIF(SUM(io_read_mb + io_write_mb), 0) AS total_io
    FROM combined
    GROUP BY server_id
)
SELECT
    c.server_id,
    c.database_name,
    c.cpu_time_ms,
    c.logical_reads,
    c.physical_reads,
    c.logical_writes,
    c.execution_count,
    c.io_read_mb,
    c.io_write_mb,
    c.io_stall_ms,
    c.cpu_time_ms * 100.0 / t.total_cpu AS pct_cpu_share,
    (c.io_read_mb + c.io_write_mb) * 100.0 / t.total_io AS pct_io_share
FROM combined c
JOIN totals t ON t.server_id = c.server_id
WHERE c.database_name IS NOT NULL
"""


_RESOURCE_USAGE = tiered(
    {
        "raw": _resource_usage_sql(
            _resource_workload_cte(collector("query_stats"), "collection_time")
        ),
        "hourly": _resource_usage_sql(
            _resource_workload_cte(rollup("query_stats_db", "hourly"), CAGG_TIME_COL)
        ),
        "daily": _resource_usage_sql(
            _resource_workload_cte(rollup("query_stats_db", "daily"), CAGG_TIME_COL)
        ),
    },
    base="query_stats_db",
)

_DB_RESOURCES_SQL = f"""
WITH u AS ({_RESOURCE_USAGE})
SELECT
    srv.name AS "Server",
    u.database_name AS "Database",
    round(u.cpu_time_ms, 0) AS "CPU Time (ms)",
    round(u.pct_cpu_share, 1) AS "CPU %",
    u.logical_reads AS "Logical Reads",
    u.physical_reads AS "Physical Reads",
    u.logical_writes AS "Logical Writes",
    u.execution_count AS "Executions",
    round(u.io_read_mb, 2) AS "IO Read MB",
    round(u.io_write_mb, 2) AS "IO Write MB",
    round(u.pct_io_share, 1) AS "IO %",
    u.io_stall_ms AS "IO Stall (ms)"
FROM u
{server_join('u.server_id')}
ORDER BY u.cpu_time_ms DESC
"""


def _latest_all_servers(relation: str) -> str:
    """Newest snapshot per server, unfiltered - the inventory section covers the whole fleet."""
    return (
        "(server_id, collection_time) IN ("
        f"SELECT server_id, MAX(collection_time) FROM {relation} GROUP BY server_id)"
    )


# ServerPropertyRow.ProductVersion - update level when known, product level otherwise.
_VERSION_DISPLAY = """CASE
        WHEN COALESCE(sp.product_update_level, '') <> ''
        THEN sp.product_version || ' - ' || sp.product_update_level
        ELSE sp.product_version || ' - ' || COALESCE(sp.product_level, '')
    END"""

# HardwareNoteFor. Both columns come from the same guarded sys.dm_os_sys_info read, so
# requiring both to be NULL avoids annotating a server over one odd NULL.
_HARDWARE_NOTE = """CASE
        WHEN sp.cpu_count IS NULL AND sp.physical_memory_mb IS NULL
        THEN 'Hardware inventory (CPU, memory, sockets) unavailable - the monitoring login '
             || 'likely lacks VIEW SERVER STATE (VIEW DATABASE STATE on Azure SQL DB) on '
             || 'this server.'
        ELSE ''
    END"""

# LicenseWarning: Standard Edition's documented CPU and RAM ceilings.
_LICENSE_WARNING = """CASE
        WHEN sp.edition NOT ILIKE '%Standard%' THEN ''
        ELSE COALESCE(NULLIF(concat_ws('; ',
            CASE WHEN sp.cpu_count > 24
                 THEN 'CPU: ' || sp.cpu_count || ' cores (Standard limited to 24)' END,
            CASE WHEN sp.physical_memory_mb > 131072
                 THEN 'RAM: ' || (sp.physical_memory_mb / 1024) || 'GB '
                      || '(Standard limited to 128GB)' END
        ), ''), '')
    END"""

# UptimeDisplay. sqlserver_start_time is server-local, compared against a local clock
# upstream too; the day/hour granularity tolerates the offset.
_UPTIME = """CASE WHEN sp.sqlserver_start_time IS NULL THEN ''
        ELSE (EXTRACT(EPOCH FROM (LOCALTIMESTAMP - sp.sqlserver_start_time))
              / 86400)::int || 'd '
             || ((EXTRACT(EPOCH FROM (LOCALTIMESTAMP - sp.sqlserver_start_time))::bigint
                  % 86400) / 3600) || 'h'
    END"""


def _yes_no(col: str) -> str:
    """HadrDisplay / ClusteredDisplay - blank when the collector could not read the flag."""
    return f"CASE WHEN {col} IS NULL THEN '' WHEN {col} THEN 'Yes' ELSE 'No' END"


_INVENTORY_STATUS = provisioning_status(
    "c.avg_cpu_pct", "c.max_cpu_pct", "c.p95_cpu_pct", "m.memory_ratio"
)

_INVENTORY_SQL = f"""
WITH props AS (
    SELECT DISTINCT ON (server_id) *
    FROM {_PROPS}
    ORDER BY server_id, collection_time DESC
),
cpu_24h AS (
    SELECT server_id,
           AVG(sqlserver_cpu_utilization::numeric) AS avg_cpu_pct,
           MAX(sqlserver_cpu_utilization) AS max_cpu_pct,
           PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY sqlserver_cpu_utilization)::numeric
               AS p95_cpu_pct
    FROM {_CPU}
    WHERE collection_time >= {UTC_NOW} - INTERVAL '24 hours'
    GROUP BY server_id
),
mem_latest AS (
    SELECT server_id,
           total_server_memory_mb::numeric / NULLIF(target_server_memory_mb, 0)
               AS memory_ratio
    FROM {_MEM}
    WHERE {_latest_all_servers(_MEM)}
),
storage_totals AS (
    SELECT server_id, SUM(total_size_mb) / 1024.0 AS total_storage_gb
    FROM {_SIZES}
    WHERE {_latest_all_servers(_SIZES)}
    GROUP BY server_id
),
sized_dbs AS (
    SELECT DISTINCT server_id, database_name
    FROM {_SIZES}
    WHERE {_latest_all_servers(_SIZES)}
      AND database_name NOT IN ({IDLE_DB_EXCLUSIONS})
),
active_dbs AS (
    SELECT DISTINCT server_id, database_name
    FROM {_QUERY_STATS}
    WHERE collection_time >= {UTC_NOW} - INTERVAL '{IDLE_DAYS} days'
      AND delta_execution_count > 0
),
idle_dbs AS (
    SELECT server_id, COUNT(*) AS idle_db_count
    FROM (SELECT * FROM sized_dbs EXCEPT SELECT * FROM active_dbs) AS idle
    GROUP BY server_id
)
SELECT
    COALESCE(reg.name, sp.server_name) AS "Server",
    sp.edition AS "Edition",
    {_VERSION_DISPLAY} AS "Version",
    COALESCE(sp.host_os_version, '') AS "Host OS",
    sp.engine_edition AS "Engine Edition",
    sp.cpu_count AS "Logical CPUs",
    sp.physical_memory_mb AS "Memory MB",
    sp.socket_count AS "Sockets",
    sp.cores_per_socket AS "Cores/Socket",
    {_HARDWARE_NOTE} AS "Hardware Note",
    {status_display(_INVENTORY_STATUS)} AS "Status",
    round(c.avg_cpu_pct, 1) AS "Avg CPU %",
    round(st.total_storage_gb, 1) AS "Storage GB",
    COALESCE(id.idle_db_count, 0) AS "Idle DBs",
    {_UPTIME} AS "Uptime",
    sp.sqlserver_start_time AS "Start Time",
    sp.collection_time AS "Last Updated",
    COALESCE(reg.monthly_cost_usd, 0) AS "Monthly ($)",
    COALESCE(reg.monthly_cost_usd, 0) * 12 AS "Annual ($)",
    /* LoadFinOpsServerInventoryAsync fixes the memory and storage terms at 80 and
       StorageScore(50) - the inventory read carries neither input. */
    {overall_score(cpu_score('COALESCE(c.avg_cpu_pct, 0)'), '80', storage_score('50'))}
        AS "Health",
    {_LICENSE_WARNING} AS "License Warning",
    {_yes_no('sp.is_hadr_enabled')} AS "HADR",
    CASE WHEN COALESCE(sp.ag_replica_role, 'Standalone') = 'Standalone'
         THEN '-' ELSE sp.ag_replica_role END AS "AG Role",
    {_yes_no('sp.is_clustered')} AS "Clustered"
FROM props sp
LEFT JOIN {SERVER_REGISTRY} reg ON reg.server_id = sp.server_id
LEFT JOIN cpu_24h c ON c.server_id = sp.server_id
LEFT JOIN mem_latest m ON m.server_id = sp.server_id
LEFT JOIN storage_totals st ON st.server_id = sp.server_id
LEFT JOIN idle_dbs id ON id.server_id = sp.server_id
ORDER BY 1
"""


def utilization_inventory():
    """Build the FinOps Utilization & Inventory dashboard."""
    reset_id()
    panels: list[dict] = []

    y = subtab(
        panels,
        "Utilization Efficiency",
        0,
        [
            (
                24,
                8,
                lambda x, y, w, h: table(
                    "Utilization Efficiency",
                    x,
                    y,
                    w,
                    h,
                    _EFFICIENCY_SQL,
                    overrides=[
                        status_colors("Status", PROVISIONING_STATUS_COLORS),
                        col_thresholds("Health", *HEALTH_SCORE_STEPS),
                        col_gauge_bar("Avg CPU %"),
                        col_gauge_bar("P95 CPU %"),
                        col_gauge_bar("Max CPU %"),
                        col_gauge_bar("Stolen Mem %"),
                        col_gauge_bar("Buffer Pool %"),
                        col_gauge_bar("Storage Free %"),
                        col_unit("Physical MB", "mbytes"),
                        col_unit("Target MB", "mbytes"),
                        col_unit("Total MB", "mbytes"),
                        col_unit("Buffer Pool MB", "mbytes"),
                        col_unit("Budget $/mo", "currencyUSD"),
                        col_unit("Budget $/yr", "currencyUSD"),
                    ],
                    description=(
                        "Fixed 24h window, as upstream's is. Health weights CPU 40 %, "
                        "memory 30 %, storage 30 %. Budget columns read 0 until one is "
                        "configured for the server."
                    ),
                ),
            ),
            (
                24,
                8,
                lambda x, y, w, h: table(
                    f"{TREND_DAYS}-Day Provisioning Trend",
                    x,
                    y,
                    w,
                    h,
                    _TREND_SQL,
                    overrides=[
                        status_colors("Status", PROVISIONING_STATUS_COLORS),
                        col_gauge_bar("Avg CPU %"),
                        col_gauge_bar("P95 CPU %"),
                        col_gauge_bar("Max CPU %"),
                    ],
                    sort_by=[{"displayName": "Day", "desc": True}],
                ),
            ),
        ],
    )

    y = subtab(
        panels,
        "Top Databases by Workload",
        y,
        [
            (
                12,
                8,
                lambda x, y, w, h: table(
                    "Top Databases by Total CPU",
                    x,
                    y,
                    w,
                    h,
                    _TOP_TOTAL_SQL,
                    overrides=[
                        col_gauge_bar("CPU %"),
                        col_gauge_bar("IO %"),
                        col_unit("CPU (ms)", "ms"),
                        col_unit("IO (MB)", "mbytes"),
                    ],
                ),
            ),
            (
                12,
                8,
                lambda x, y, w, h: table(
                    "Top Databases by Avg CPU / Execution",
                    x,
                    y,
                    w,
                    h,
                    _TOP_AVG_SQL,
                    overrides=[
                        col_unit("Avg CPU (ms)", "ms"),
                        col_unit("Avg IO (MB)", "mbytes"),
                        col_unit("Total CPU (ms)", "ms"),
                        col_unit("Total IO (MB)", "mbytes"),
                    ],
                ),
            ),
        ],
    )

    y = subtab(
        panels,
        "Database Sizes - Allocated vs Used",
        y,
        [
            (
                24,
                9,
                lambda x, y, w, h: table(
                    "Database Sizes - Allocated vs Used",
                    x,
                    y,
                    w,
                    h,
                    _DB_SIZE_SQL,
                    overrides=[
                        col_unit("Used MB", "mbytes"),
                        col_unit("Allocated MB", "mbytes"),
                        col_gauge_bar("Used %"),
                    ],
                    description=(
                        f"Top {_SIZE_TOP_N} databases per server by allocated size."
                    ),
                ),
            )
        ],
    )

    y = subtab(
        panels,
        "Database Resource Usage",
        y,
        [
            (
                24,
                20,
                lambda x, y, w, h: table(
                    "Database Resource Usage",
                    x,
                    y,
                    w,
                    h,
                    _DB_RESOURCES_SQL,
                    overrides=[
                        col_unit("CPU Time (ms)", "ms"),
                        col_gauge_bar("CPU %"),
                        col_gauge_bar("IO %"),
                        col_unit("IO Read MB", "mbytes"),
                        col_unit("IO Write MB", "mbytes"),
                        col_unit("IO Stall (ms)", "ms"),
                    ],
                    sort_by=[{"displayName": "CPU Time (ms)", "desc": True}],
                    description=(
                        "Over the dashboard range. CPU % and IO % are shares of that "
                        "server's own total."
                    ),
                ),
            )
        ],
    )

    subtab(
        panels,
        "Server Inventory",
        y,
        [
            (
                24,
                20,
                lambda x, y, w, h: table(
                    "Server Inventory (All Servers)",
                    x,
                    y,
                    w,
                    h,
                    _INVENTORY_SQL,
                    overrides=[
                        status_colors("Status", PROVISIONING_STATUS_COLORS),
                        col_thresholds("Health", *HEALTH_SCORE_STEPS),
                        col_gauge_bar("Avg CPU %"),
                        col_unit("Memory MB", "mbytes"),
                        col_unit("Storage GB", "gbytes"),
                        col_unit("Monthly ($)", "currencyUSD"),
                        col_unit("Annual ($)", "currencyUSD"),
                        col_thresholds("Idle DBs", ("text", None), ("yellow", 1)),
                    ],
                    description=(
                        "Every registered server, not filtered by $server. Health varies "
                        "only with CPU: upstream fixes the memory and storage terms here."
                    ),
                ),
            )
        ],
    )

    return finops_dashboard(
        uid("finops-utilization-inventory"),
        "Utilization & Inventory",
        panels,
        [server_var()],
    )
