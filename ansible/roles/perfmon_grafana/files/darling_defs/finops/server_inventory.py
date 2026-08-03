"""FinOps Server Inventory dashboard (Darling line).

Upstream ref: ViewerDataService.FinOps.Inventory.cs.

Cross-server by design - the table carries no $server filter, listing every registered
server regardless of the dashboard's $server selection.
"""

from .._shared import (
    SERVER_REGISTRY,
    UTC_NOW,
    col_gauge_bar,
    col_thresholds,
    col_unit,
    collector,
    finops_dashboard,
    reset_id,
    server_var,
    status_colors,
    subtab,
    table,
    uid,
)
from ._shared import (
    HEALTH_SCORE_STEPS,
    IDLE_DAYS,
    IDLE_DB_EXCLUSIONS,
    PROVISIONING_STATUS_COLORS,
    cpu_score,
    overall_score,
    provisioning_status,
    status_display,
    storage_score,
)

_PROPS = collector("server_properties")
_CPU = collector("cpu_utilization_stats")
_MEM = collector("memory_stats")
_SIZES = collector("database_size_stats")
_QUERY_STATS = collector("query_stats")


def _latest_all_servers(relation: str) -> str:
    """Newest snapshot per server, unfiltered - this dashboard covers the whole fleet."""
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


def server_inventory():
    """Build the FinOps Server Inventory dashboard."""
    reset_id()
    panels: list[dict] = []

    subtab(
        panels,
        "Server Inventory",
        0,
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
        uid("finops-server-inventory"),
        "Server Inventory",
        panels,
        [server_var()],
    )
