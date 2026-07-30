"""FinOps Optimization dashboard (Darling line).

Upstream ref: IdleDatabasesSql / TempdbSummarySql (ViewerDataService.FinOps.Storage.cs),
WaitCategorySummarySql / ExpensiveQueriesSql (.Workload.cs), MemoryGrantEfficiencySql
(.Utilization.cs), FinOpsTab.Loaders.cs.

One collapsible row per upstream Expander. Wait Stats Summary and Expensive Queries follow the
dashboard range; the other three keep upstream's fixed windows.
"""

from .._shared import (
    RAW_MAX_AGE_DAYS,
    UTC_NOW,
    col_gauge_bar,
    col_thresholds,
    col_unit,
    collector,
    finops_dashboard,
    reset_id,
    server_filter,
    server_join,
    server_var,
    subtab,
    table,
    uid,
)
from ._shared import (
    IDLE_DAYS,
    budget_cte,
    fixed_window_tiers,
    idle_db_ctes,
    wait_category,
    window_budget,
)

_TEMPDB = collector("tempdb_stats")
_WAITS = collector("wait_stats")
_QUERY_STATS = collector("query_stats")
_GRANTS = collector("memory_grant_stats")


def _idle_sql(relation: str, time_col: str) -> str:
    """The idle-database grid for one retention tier."""
    return f"""
WITH {idle_db_ctes(relation, time_col, with_details=True)}
SELECT server_id, database_name, total_size_mb, file_count, last_execution
FROM idle_dbs
"""


_IDLE_SQL = f"""
WITH i AS ({fixed_window_tiers(_idle_sql, IDLE_DAYS)})
SELECT
    srv.name AS "Server",
    i.database_name AS "Database",
    round(i.total_size_mb, 2) AS "Total Size MB",
    i.file_count AS "Files",
    i.last_execution AS "Last Execution"
FROM i
{server_join('i.server_id')}
ORDER BY i.total_size_mb DESC
"""

# TempdbSummarySql. The VALUES spine preserves upstream's metric order.
_TEMPDB_SQL = f"""
WITH latest AS (
    SELECT DISTINCT ON (server_id)
           server_id, user_object_reserved_mb, internal_object_reserved_mb,
           version_store_reserved_mb, total_reserved_mb
    FROM {_TEMPDB}
    WHERE {server_filter()}
    ORDER BY server_id, collection_time DESC
),
peak AS (
    SELECT server_id,
           MAX(user_object_reserved_mb) AS max_user_mb,
           MAX(internal_object_reserved_mb) AS max_internal_mb,
           MAX(version_store_reserved_mb) AS max_version_store_mb,
           MAX(total_reserved_mb) AS max_total_mb
    FROM {_TEMPDB}
    WHERE {server_filter()} AND collection_time >= {UTC_NOW} - INTERVAL '24 hours'
    GROUP BY server_id
)
SELECT
    srv.name AS "Server",
    m.metric AS "Metric",
    round(m.current_mb, 2) AS "Current MB",
    round(m.peak_mb, 2) AS "Peak 24h MB",
    m.warning AS "Warning"
FROM latest l
JOIN peak p ON p.server_id = l.server_id
{server_join('l.server_id')}
CROSS JOIN LATERAL (VALUES
    ('User Objects', l.user_object_reserved_mb, p.max_user_mb,
     CASE WHEN p.max_user_mb > 1024 THEN 'High user object usage' ELSE '' END),
    ('Internal Objects', l.internal_object_reserved_mb, p.max_internal_mb,
     CASE WHEN p.max_internal_mb > 1024
          THEN 'High internal object usage (sorts/hashes)' ELSE '' END),
    ('Version Store', l.version_store_reserved_mb, p.max_version_store_mb,
     CASE WHEN p.max_version_store_mb > 2048
          THEN 'Version store pressure - check long-running transactions' ELSE '' END),
    ('Total Reserved', l.total_reserved_mb, p.max_total_mb, '')
) AS m(metric, current_mb, peak_mb, warning)
ORDER BY srv.name, m.metric
"""

_CATEGORY = wait_category("ws.wait_type")

_WAITS_SQL = f"""
WITH categorized AS (
    SELECT ws.server_id,
           {_CATEGORY} AS category,
           ws.wait_type,
           SUM(ws.delta_wait_time_ms) AS wait_time_ms,
           SUM(ws.delta_waiting_tasks) AS waiting_tasks
    FROM {_WAITS} AS ws
    WHERE {server_filter('ws.server_id')}
      AND $__timeFilter(ws.collection_time)
      AND ws.delta_wait_time_ms IS NOT NULL
      AND ws.delta_wait_time_ms > 0
    GROUP BY ws.server_id, {_CATEGORY}, ws.wait_type
),
ranked AS (
    SELECT c.*,
           ROW_NUMBER() OVER (
               PARTITION BY c.server_id, c.category ORDER BY c.wait_time_ms DESC) AS rn
    FROM categorized c
),
by_category AS (
    SELECT server_id, category,
           SUM(wait_time_ms) AS total_wait_time_ms,
           SUM(waiting_tasks) AS total_waiting_tasks,
           MAX(CASE WHEN rn = 1 THEN wait_type END) AS top_wait_type,
           MAX(CASE WHEN rn = 1 THEN wait_time_ms END) AS top_wait_time_ms
    FROM ranked
    GROUP BY server_id, category
),
grand_total AS (
    SELECT server_id, NULLIF(SUM(total_wait_time_ms), 0) AS total
    FROM by_category
    GROUP BY server_id
),
{budget_cte()}
SELECT
    srv.name AS "Server",
    bc.category AS "Category",
    bc.total_wait_time_ms AS "Total Wait ms",
    bc.total_waiting_tasks AS "Waiting Tasks",
    round(bc.total_wait_time_ms * 100.0 / gt.total, 1) AS "% of Total",
    bc.top_wait_type AS "Top Wait Type",
    bc.top_wait_time_ms AS "Top Wait ms",
    round(COALESCE(
        bc.total_wait_time_ms::numeric / gt.total * {window_budget()}, 0), 2)
        AS "Est. Cost ($)"
FROM by_category bc
JOIN grand_total gt ON gt.server_id = bc.server_id
LEFT JOIN budget b ON b.server_id = bc.server_id
{server_join('bc.server_id')}
ORDER BY bc.total_wait_time_ms DESC
"""

# ExpensiveQueriesSql's topN.
_EXPENSIVE_TOP_N = 20

# Query text lives only in raw, so this panel cannot route. Upstream clamps to RawTextHorizon
# and labels the clamp; the GREATEST below is that clamp.
_TEXT_HORIZON = f"{UTC_NOW} - INTERVAL '{RAW_MAX_AGE_DAYS} days'"

_EXPENSIVE_SQL = f"""
WITH top_queries AS (
    SELECT qs.server_id,
           qs.database_name,
           SUM(qs.delta_worker_time) / 1000.0 AS total_cpu_ms,
           SUM(qs.delta_worker_time) / 1000.0
               / NULLIF(SUM(qs.delta_execution_count), 0) AS avg_cpu_ms,
           SUM(qs.delta_logical_reads) AS total_reads,
           SUM(qs.delta_logical_reads)::numeric
               / NULLIF(SUM(qs.delta_execution_count), 0) AS avg_reads,
           SUM(qs.delta_execution_count) AS executions,
           left(qs.query_text, 200) AS query_preview,
           qs.query_text AS full_query_text,
           ROW_NUMBER() OVER (
               PARTITION BY qs.server_id
               ORDER BY SUM(qs.delta_worker_time) DESC) AS rn
    FROM {_QUERY_STATS} AS qs
    WHERE {server_filter('qs.server_id')}
      AND qs.collection_time >= GREATEST($__timeFrom()::timestamp, {_TEXT_HORIZON})
      AND qs.collection_time <= $__timeTo()::timestamp
      AND qs.delta_worker_time IS NOT NULL
      AND qs.delta_worker_time > 0
    GROUP BY qs.server_id, qs.database_name, qs.sql_handle, qs.query_text
),
kept AS (
    SELECT * FROM top_queries WHERE rn <= {_EXPENSIVE_TOP_N}
),
totals AS (
    SELECT server_id, NULLIF(SUM(total_cpu_ms), 0) AS total_cpu
    FROM kept
    GROUP BY server_id
),
{budget_cte()}
SELECT
    srv.name AS "Server",
    k.database_name AS "Database",
    round(k.total_cpu_ms, 0) AS "Total CPU ms",
    round(k.avg_cpu_ms, 2) AS "Avg CPU/Exec",
    k.total_reads AS "Total Reads",
    round(k.avg_reads, 0) AS "Avg Reads/Exec",
    k.executions AS "Executions",
    round(COALESCE(k.total_cpu_ms / t.total_cpu * {window_budget()}, 0), 2)
        AS "Est. Cost ($)",
    k.query_preview AS "Query Preview",
    k.full_query_text AS "Query Text"
FROM kept k
JOIN totals t ON t.server_id = k.server_id
LEFT JOIN budget b ON b.server_id = k.server_id
{server_join('k.server_id')}
ORDER BY k.total_cpu_ms DESC
"""

# MemoryGrantEfficiencySql. WastedMb is a computed property upstream, not a column. Note the
# _delta SUFFIX on the two cumulative columns; the rest are point-in-time gauges.
_GRANTS_SQL = f"""
SELECT
    mg.collection_time::date AS "Day",
    srv.name AS "Server",
    round(AVG(mg.granted_memory_mb), 1) AS "Avg Granted MB",
    round(AVG(mg.used_memory_mb), 1) AS "Avg Used MB",
    round(AVG(mg.used_memory_mb) * 100.0 / NULLIF(AVG(mg.granted_memory_mb), 0), 1)
        AS "Efficiency %",
    round(MAX(mg.granted_memory_mb), 1) AS "Peak Grant MB",
    round(AVG(mg.granted_memory_mb) - AVG(mg.used_memory_mb), 1) AS "Wasted MB",
    SUM(mg.grantee_count) AS "Grantees",
    SUM(mg.waiter_count) AS "Waiters",
    SUM(mg.timeout_error_count_delta) AS "Timeouts",
    SUM(mg.forced_grant_count_delta) AS "Forced"
FROM {_GRANTS} AS mg
{server_join('mg.server_id')}
WHERE {server_filter('mg.server_id')}
  AND mg.collection_time >= {UTC_NOW} - INTERVAL '24 hours'
GROUP BY mg.collection_time::date, srv.name
ORDER BY mg.collection_time::date
"""


def optimization():
    """Build the FinOps Optimization dashboard."""
    reset_id()
    panels: list[dict] = []

    y = subtab(
        panels,
        "Idle Databases",
        0,
        [
            (
                24,
                8,
                lambda x, y, w, h: table(
                    f"Idle Databases (no activity in {IDLE_DAYS} days)",
                    x,
                    y,
                    w,
                    h,
                    _IDLE_SQL,
                    overrides=[col_unit("Total Size MB", "mbytes")],
                    sort_by=[{"displayName": "Total Size MB", "desc": True}],
                    description=(
                        f"Fixed {IDLE_DAYS}-day window. System databases and "
                        "PerformanceMonitor are never counted. Last Execution is the "
                        "monitored server's local clock."
                    ),
                ),
            )
        ],
    )

    y = subtab(
        panels,
        "tempdb Pressure",
        y,
        [
            (
                24,
                8,
                lambda x, y, w, h: table(
                    "tempdb Pressure",
                    x,
                    y,
                    w,
                    h,
                    _TEMPDB_SQL,
                    overrides=[
                        col_unit("Current MB", "mbytes"),
                        col_unit("Peak 24h MB", "mbytes"),
                    ],
                ),
            )
        ],
    )

    y = subtab(
        panels,
        "Wait Stats Summary",
        y,
        [
            (
                24,
                8,
                lambda x, y, w, h: table(
                    "Wait Stats Summary",
                    x,
                    y,
                    w,
                    h,
                    _WAITS_SQL,
                    overrides=[
                        col_unit("Total Wait ms", "ms"),
                        col_unit("Top Wait ms", "ms"),
                        col_gauge_bar("% of Total"),
                        col_unit("Est. Cost ($)", "currencyUSD"),
                    ],
                    sort_by=[{"displayName": "Total Wait ms", "desc": True}],
                    description=(
                        "Est. Cost is the category's share of the budget, prorated to the "
                        "dashboard range."
                    ),
                ),
            )
        ],
    )

    y = subtab(
        panels,
        f"Expensive Queries (Top {_EXPENSIVE_TOP_N} by CPU)",
        y,
        [
            (
                24,
                10,
                lambda x, y, w, h: table(
                    f"Expensive Queries (Top {_EXPENSIVE_TOP_N} by CPU)",
                    x,
                    y,
                    w,
                    h,
                    _EXPENSIVE_SQL,
                    overrides=[
                        col_unit("Total CPU ms", "ms"),
                        col_unit("Avg CPU/Exec", "ms"),
                        col_unit("Est. Cost ($)", "currencyUSD"),
                    ],
                    sort_by=[{"displayName": "Total CPU ms", "desc": True}],
                    description=(
                        f"Query text lives only in the raw tier, so this reaches back at "
                        f"most {RAW_MAX_AGE_DAYS} days however wide the range. Upstream's "
                        "stored-plan viewer has no Grafana equivalent."
                    ),
                ),
            )
        ],
    )

    subtab(
        panels,
        "Memory Grant Efficiency",
        y,
        [
            (
                24,
                8,
                lambda x, y, w, h: table(
                    "Memory Grant Efficiency",
                    x,
                    y,
                    w,
                    h,
                    _GRANTS_SQL,
                    overrides=[
                        col_unit("Avg Granted MB", "mbytes"),
                        col_unit("Avg Used MB", "mbytes"),
                        col_unit("Peak Grant MB", "mbytes"),
                        col_unit("Wasted MB", "mbytes"),
                        col_gauge_bar("Efficiency %"),
                        col_thresholds("Timeouts", ("text", None), ("red", 1)),
                        col_thresholds("Forced", ("text", None), ("yellow", 1)),
                    ],
                    sort_by=[{"displayName": "Day", "desc": True}],
                ),
            )
        ],
    )

    return finops_dashboard(
        uid("finops-optimization"),
        "FinOps - Optimization",
        panels,
        [server_var()],
    )
