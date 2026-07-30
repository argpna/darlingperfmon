"""FinOps Database Resources dashboard (Darling line).

Upstream ref: DatabaseResourceUsageSql, WorkloadCteRaw / WorkloadCteForCagg
(ViewerDataService.FinOps.Workload.cs).

The only reader needing query_stats_db_*: it sums the I/O columns, which the query-grain
rollup does not carry.
"""

from .._shared import (
    CAGG_TIME_COL,
    col_gauge_bar,
    col_unit,
    collector,
    finops_dashboard,
    flow,
    reset_id,
    rollup,
    server_filter,
    server_join,
    server_var,
    table,
    tiered,
    uid,
)

_FILE_IO = collector("file_io_stats")


def _workload_cte(relation: str, time_col: str) -> str:
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


def _usage_sql(workload_cte: str) -> str:
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


_USAGE = tiered(
    {
        "raw": _usage_sql(_workload_cte(collector("query_stats"), "collection_time")),
        "hourly": _usage_sql(
            _workload_cte(rollup("query_stats_db", "hourly"), CAGG_TIME_COL)
        ),
        "daily": _usage_sql(
            _workload_cte(rollup("query_stats_db", "daily"), CAGG_TIME_COL)
        ),
    },
    base="query_stats_db",
)

_SQL = f"""
WITH u AS ({_USAGE})
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


def database_resources():
    """Build the FinOps Database Resources dashboard."""
    reset_id()
    panels: list[dict] = []

    flow(
        panels,
        0,
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
                    _SQL,
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

    return finops_dashboard(
        uid("finops-database-resources"),
        "Database Resources",
        panels,
        [server_var()],
    )
