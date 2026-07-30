"""Queries dashboard (Darling line).

Upstream ref: ViewerServerTab.Queries.cs, ViewerDataService.QueryStats.cs, .ProcedureStats.cs,
.QueryStore.cs, .QueryStoreRegressions.cs, .QuerySnapshots.cs, .QueryTrends.cs,
.QueryHeatmap.cs (Darling.Viewer). Seven sub-tabs, matching upstream order. Current Active
Queries (LIVE) is not ported: it needs a live DMV connection to the monitored server via
config_command, and Grafana here talks only to Postgres.

The Top Queries/Procedures/Query Store grids and their comparison panels stay raw-table-only,
not tiered() - the hourly/daily CAGGs carry only cpu/duration/execution-count sums, not the
other columns these grids show. Only the four Performance Trends charts and each grid's
fixed-metric CPU trend use tiered().
"""

from ._shared import (
    CAGG_TIME_COL,
    col_gauge_bar,
    collector,
    custom_var,
    dashboard,
    flow,
    heatmap,
    multi_filter,
    query_var,
    reset_id,
    rollup,
    server_filter,
    server_join,
    server_var,
    status_colors,
    subtab,
    table,
    target,
    tiered,
    timeseries,
    uid,
)

_HOURLY_SECONDS = 3600.0
_DAILY_SECONDS = 86400.0

_DATABASE_VAR_SQL = f"""
SELECT DISTINCT database_name
FROM {collector('query_stats')}
WHERE {server_filter()}
ORDER BY 1
"""

_COMPARISON_WINDOW_CTE = """
_window AS (
    SELECT
        $__timeFrom()::timestamp AS cur_start,
        $__timeTo()::timestamp AS cur_end,
        $__timeFrom()::timestamp - (CASE ${comparison_baseline:sqlstring}
            WHEN 'Last Week' THEN INTERVAL '7 days' ELSE INTERVAL '1 day' END) AS base_start,
        $__timeTo()::timestamp - (CASE ${comparison_baseline:sqlstring}
            WHEN 'Last Week' THEN INTERVAL '7 days' ELSE INTERVAL '1 day' END) AS base_end
)
"""


# Performance Trends: 4 tiered per-second-rate charts.
# Upstream ref: QueryDurationTrendSql / ProcedureDurationTrendSql / QueryStoreDurationTrendSql /
# ExecutionCountTrendSql (ViewerDataService.QueryTrends.cs). Raw mirrors upstream's per-snapshot
# LAG(collection_time) rate exactly (partitioned by server for a multi-$server selection);
# hourly/daily divide the CAGG bucket's pre-summed metric by the bucket's fixed width instead,
# since a CAGG bucket has no "previous snapshot" to LAG against.
def _trend_sql(base, raw_value_expr, hourly_value_expr, daily_value_expr):
    """Tiered rate trend, grouped/labelled per server via server_join()."""
    raw_sql = f"""
SELECT
    t.time AS time,
    srv.name AS metric,
    CASE WHEN t.interval_seconds > 0 THEN t.raw_value / t.interval_seconds ELSE 0 END AS value
FROM (
    SELECT
        server_id,
        collection_time AS time,
        {raw_value_expr} AS raw_value,
        extract(epoch FROM (
            date_trunc('second', collection_time)
            - date_trunc('second', LAG(collection_time) OVER (PARTITION BY server_id ORDER BY collection_time))
        )) AS interval_seconds
    FROM {collector(base)}
    WHERE $__timeFilter(collection_time)
      AND {server_filter()}
    GROUP BY server_id, collection_time
) AS t
{server_join('t.server_id')}
"""
    hourly_sql = f"""
SELECT
    b.{CAGG_TIME_COL} AS time,
    srv.name AS metric,
    {hourly_value_expr} AS value
FROM {rollup(base, 'hourly')} AS b
{server_join('b.server_id')}
WHERE $__timeFilter(b.{CAGG_TIME_COL})
  AND {server_filter('b.server_id')}
GROUP BY b.{CAGG_TIME_COL}, srv.name
"""
    daily_sql = f"""
SELECT
    b.{CAGG_TIME_COL} AS time,
    srv.name AS metric,
    {daily_value_expr} AS value
FROM {rollup(base, 'daily')} AS b
{server_join('b.server_id')}
WHERE $__timeFilter(b.{CAGG_TIME_COL})
  AND {server_filter('b.server_id')}
GROUP BY b.{CAGG_TIME_COL}, srv.name
"""
    return tiered({"raw": raw_sql, "hourly": hourly_sql, "daily": daily_sql}, base=base)


_QUERY_DURATION_TREND_SQL = _trend_sql(
    "query_stats",
    "SUM(delta_elapsed_time) / 1000.0",
    f"SUM(b.elapsed_time_sum) / 1000.0 / {_HOURLY_SECONDS}",
    f"SUM(b.elapsed_time_sum) / 1000.0 / {_DAILY_SECONDS}",
)
_PROCEDURE_DURATION_TREND_SQL = _trend_sql(
    "procedure_stats",
    "SUM(delta_elapsed_time) / 1000.0",
    f"SUM(b.elapsed_time_sum) / 1000.0 / {_HOURLY_SECONDS}",
    f"SUM(b.elapsed_time_sum) / 1000.0 / {_DAILY_SECONDS}",
)
_QUERY_STORE_DURATION_TREND_SQL = _trend_sql(
    "query_store_stats",
    "SUM(execution_count * avg_duration_us) / 1000.0",
    f"SUM(b.duration_us_weighted_sum) / 1000.0 / {_HOURLY_SECONDS}",
    f"SUM(b.duration_us_weighted_sum) / 1000.0 / {_DAILY_SECONDS}",
)
_EXECUTION_COUNT_TREND_SQL = _trend_sql(
    "query_stats",
    "SUM(delta_execution_count)",
    f"SUM(b.execution_count_sum) / {_HOURLY_SECONDS}",
    f"SUM(b.execution_count_sum) / {_DAILY_SECONDS}",
)


# Fixed-metric (Total CPU) hourly trend: the slicer's static substitute.
def _slicer_sql(base, raw_expr, hourly_expr, daily_expr):
    """Tiered hourly-bucket Total CPU trend, the fixed default metric a slicer opens on."""
    raw_sql = f"""
SELECT
    date_trunc('hour', t.collection_time) AS time,
    srv.name AS metric,
    {raw_expr} AS value
FROM {collector(base)} AS t
{server_join('t.server_id')}
WHERE $__timeFilter(t.collection_time)
  AND {server_filter('t.server_id')}
  AND {multi_filter('t.database_name', 'database')}
GROUP BY date_trunc('hour', t.collection_time), srv.name
"""
    hourly_sql = f"""
SELECT
    date_trunc('hour', b.{CAGG_TIME_COL}) AS time,
    srv.name AS metric,
    {hourly_expr} AS value
FROM {rollup(base, 'hourly')} AS b
{server_join('b.server_id')}
WHERE $__timeFilter(b.{CAGG_TIME_COL})
  AND {server_filter('b.server_id')}
  AND {multi_filter('b.database_name', 'database')}
GROUP BY date_trunc('hour', b.{CAGG_TIME_COL}), srv.name
"""
    daily_sql = f"""
SELECT
    date_trunc('hour', b.{CAGG_TIME_COL}) AS time,
    srv.name AS metric,
    {daily_expr} AS value
FROM {rollup(base, 'daily')} AS b
{server_join('b.server_id')}
WHERE $__timeFilter(b.{CAGG_TIME_COL})
  AND {server_filter('b.server_id')}
  AND {multi_filter('b.database_name', 'database')}
GROUP BY date_trunc('hour', b.{CAGG_TIME_COL}), srv.name
"""
    return tiered({"raw": raw_sql, "hourly": hourly_sql, "daily": daily_sql}, base=base)


_TOP_QUERIES_SLICER_SQL = _slicer_sql(
    "query_stats",
    "SUM(t.delta_worker_time) / 1000.0",
    "SUM(b.worker_time_sum) / 1000.0",
    "SUM(b.worker_time_sum) / 1000.0",
)
_TOP_PROCEDURES_SLICER_SQL = _slicer_sql(
    "procedure_stats",
    "SUM(t.delta_worker_time) / 1000.0",
    "SUM(b.worker_time_sum) / 1000.0",
    "SUM(b.worker_time_sum) / 1000.0",
)
_QUERY_STORE_SLICER_SQL = _slicer_sql(
    "query_store_stats",
    "SUM(t.execution_count * t.avg_cpu_time_us::double precision) / 1000.0",
    "SUM(b.cpu_us_weighted_sum) / 1000.0",
    "SUM(b.cpu_us_weighted_sum) / 1000.0",
)


# Active Queries: stored sp_WhoIsActive-style snapshots (collect.query_snapshots).
# Upstream ref: LatestQuerySnapshotsSql (ViewerDataService.QuerySnapshots.cs).
_ACTIVE_QUERIES_SQL = f"""
SELECT
    srv.name AS "Server",
    qs.session_id AS "SPID",
    qs.collection_time AS "Collected",
    qs.database_name AS "Database",
    qs.login_name AS "Login",
    qs.host_name AS "Host",
    qs.program_name AS "Program",
    qs.status AS "Status",
    qs.elapsed_time_formatted AS "Elapsed",
    qs.cpu_time_ms AS "CPU (ms)",
    qs.logical_reads AS "Logical Reads",
    qs.reads AS "Reads",
    qs.writes AS "Writes",
    qs.wait_type AS "Wait Type",
    qs.wait_time_ms AS "Wait (ms)",
    qs.wait_resource AS "Wait Resource",
    qs.blocking_session_id AS "Blocking",
    qs.dop AS "DOP",
    qs.parallel_worker_count AS "Workers",
    qs.granted_query_memory_gb AS "Memory (GB)",
    qs.transaction_isolation_level AS "Isolation",
    qs.open_transaction_count AS "Open Tran",
    qs.percent_complete AS "% Done",
    qs.tran_start_time AS "Tran Start",
    qs.query_hash AS "Query Hash",
    (qs.query_plan IS NOT NULL) AS "Has Query Plan",
    (qs.live_query_plan IS NOT NULL) AS "Has Live Query Plan",
    qs.query_text AS "Query Text"
FROM {collector('query_snapshots')} AS qs
{server_join('qs.server_id')}
WHERE {server_filter('qs.server_id')}
  AND $__timeFilter(qs.collection_time)
  AND {multi_filter('qs.database_name', 'database')}
  AND qs.query_text NOT LIKE 'WAITFOR%'
ORDER BY qs.collection_time DESC, qs.cpu_time_ms DESC
"""

# Upstream ref: ActiveQuerySlicerSql (ViewerDataService.QuerySnapshots.cs) - default metric is
# session count. query_snapshots has no continuous aggregate (not in _CAGG_DIMENSIONS), so this
# stays raw-only; its own retention window is short anyway (see _shared.py's retention note).
_ACTIVE_QUERIES_SLICER_SQL = f"""
SELECT
    date_trunc('hour', qs.collection_time) AS time,
    srv.name AS metric,
    COUNT(*) AS value
FROM {collector('query_snapshots')} AS qs
{server_join('qs.server_id')}
WHERE $__timeFilter(qs.collection_time)
  AND {server_filter('qs.server_id')}
  AND {multi_filter('qs.database_name', 'database')}
GROUP BY date_trunc('hour', qs.collection_time), srv.name
"""


# Top Queries by Duration.
# Upstream ref: TopQueriesSql (ViewerDataService.QueryStats.cs). The #1568 module-attribution
# CTE is adapted for multi-server: upstream reads one server at a time so a bare sql_handle key
# is enough; here it is partitioned/joined on (server_id, sql_handle) too, since a handle is only
# unique within one plan cache.
_TOP_QUERIES_SQL = f"""
WITH ranked AS (
    SELECT
        qs.server_id,
        qs.database_name,
        qs.query_hash,
        MAX(qs.last_execution_time) AS last_execution_time,
        MAX(qs.creation_time) AS creation_time,
        SUM(qs.delta_execution_count)::bigint AS total_executions,
        SUM(qs.delta_worker_time)::bigint AS total_cpu_us,
        SUM(qs.delta_elapsed_time)::bigint AS total_elapsed_us,
        SUM(qs.delta_logical_reads)::bigint AS total_reads,
        SUM(qs.delta_rows)::bigint AS total_rows,
        SUM(qs.delta_logical_writes)::bigint AS total_writes,
        SUM(qs.delta_physical_reads)::bigint AS total_physical_reads,
        SUM(qs.delta_spills)::bigint AS total_spills,
        MIN(qs.min_dop) AS min_dop,
        MAX(qs.max_dop) AS max_dop,
        MIN(qs.min_worker_time) AS min_worker_time,
        MAX(qs.max_worker_time) AS max_worker_time,
        MIN(qs.min_elapsed_time) AS min_elapsed_time,
        MAX(qs.max_elapsed_time) AS max_elapsed_time,
        MIN(qs.min_physical_reads) AS min_physical_reads,
        MAX(qs.max_physical_reads) AS max_physical_reads,
        MIN(qs.min_rows) AS min_rows,
        MAX(qs.max_rows) AS max_rows,
        MIN(qs.min_grant_kb) AS min_grant_kb,
        MAX(qs.max_grant_kb) AS max_grant_kb,
        MIN(qs.min_used_grant_kb) AS min_used_grant_kb,
        MAX(qs.max_used_grant_kb) AS max_used_grant_kb,
        MIN(qs.min_ideal_grant_kb) AS min_ideal_grant_kb,
        MAX(qs.max_ideal_grant_kb) AS max_ideal_grant_kb,
        MIN(qs.min_reserved_threads) AS min_reserved_threads,
        MAX(qs.max_reserved_threads) AS max_reserved_threads,
        MIN(qs.min_used_threads) AS min_used_threads,
        MAX(qs.max_used_threads) AS max_used_threads,
        MIN(qs.min_spills) AS min_spills,
        MAX(qs.max_spills) AS max_spills,
        MAX(qs.query_plan_hash) AS query_plan_hash,
        MAX(qs.sql_handle) AS sql_handle,
        MAX(qs.plan_handle) AS plan_handle,
        MAX(qs.total_clr_time) AS total_clr_time,
        MAX(qs.plan_generation_num) AS plan_generation_num,
        MAX(qs.delta_worker_time::double precision / NULLIF(qs.sample_interval_seconds, 0) / 1000.0) AS worker_time_per_second,
        bool_or(qs.query_plan_xml IS NOT NULL OR qs.query_plan_digest IS NOT NULL) AS has_query_plan
    FROM {collector('query_stats')} AS qs
    WHERE {server_filter('qs.server_id')}
      AND $__timeFilter(qs.collection_time)
      AND {multi_filter('qs.database_name', 'database')}
    GROUP BY qs.server_id, qs.database_name, qs.query_hash
    HAVING SUM(qs.delta_execution_count) > 0 OR SUM(qs.delta_elapsed_time) > 0
    ORDER BY SUM(qs.delta_elapsed_time) DESC
    LIMIT ${{topn}} + 5
),
module AS (
    SELECT server_id, sql_handle, object_name, schema_name, database_name
    FROM (
        SELECT
            server_id, sql_handle, object_name, schema_name, database_name,
            ROW_NUMBER() OVER (PARTITION BY server_id, sql_handle ORDER BY collection_time DESC) AS rn
        FROM {collector('procedure_stats')}
        WHERE {server_filter()}
          AND sql_handle IS NOT NULL AND sql_handle <> ''
    ) AS ranked_modules
    WHERE rn = 1
)
SELECT
    srv.name AS "Server",
    r.database_name AS "Database",
    COALESCE(m.database_name || '.' || m.schema_name || '.' || m.object_name, 'ad hoc') AS "Module",
    r.last_execution_time AS "Last Execution",
    r.creation_time AS "Creation Time",
    r.total_executions AS "Executions",
    r.total_cpu_us / 1000.0 AS "Total CPU (ms)",
    (r.total_cpu_us / 1000.0) / NULLIF(r.total_executions, 0) AS "Avg CPU (ms)",
    r.worker_time_per_second AS "Peak CPU (ms/s)",
    r.plan_generation_num AS "Plan Gen",
    r.total_clr_time / 1000.0 AS "Total CLR (ms)",
    r.total_elapsed_us / 1000.0 AS "Total Duration (ms)",
    (r.total_elapsed_us / 1000.0) / NULLIF(r.total_executions, 0) AS "Avg Duration (ms)",
    r.total_reads AS "Total Reads",
    r.total_reads::double precision / NULLIF(r.total_executions, 0) AS "Avg Reads",
    r.total_writes AS "Total Writes",
    r.total_physical_reads AS "Physical Reads",
    r.total_rows AS "Total Rows",
    r.total_spills AS "Total Spills",
    r.min_worker_time / 1000.0 AS "Min CPU (ms)",
    r.max_worker_time / 1000.0 AS "Max CPU (ms)",
    r.min_elapsed_time / 1000.0 AS "Min Duration (ms)",
    r.max_elapsed_time / 1000.0 AS "Max Duration (ms)",
    r.min_physical_reads AS "Min Phys Reads",
    r.max_physical_reads AS "Max Phys Reads",
    r.min_rows AS "Min Rows",
    r.max_rows AS "Max Rows",
    r.min_grant_kb AS "Min Grant (KB)",
    r.max_grant_kb AS "Max Grant (KB)",
    r.min_used_grant_kb AS "Min Used Grant (KB)",
    r.max_used_grant_kb AS "Max Used Grant (KB)",
    r.min_ideal_grant_kb AS "Min Ideal Grant (KB)",
    r.max_ideal_grant_kb AS "Max Ideal Grant (KB)",
    r.min_spills AS "Min Spills",
    r.max_spills AS "Max Spills",
    r.min_dop AS "Min DOP",
    r.max_dop AS "Max DOP",
    r.min_reserved_threads AS "Min Reserved Threads",
    r.max_reserved_threads AS "Max Reserved Threads",
    r.min_used_threads AS "Min Used Threads",
    r.max_used_threads AS "Max Used Threads",
    r.query_hash AS "Query Hash",
    r.query_plan_hash AS "Plan Hash",
    r.sql_handle AS "SQL Handle",
    r.plan_handle AS "Plan Handle",
    r.has_query_plan AS "Has Query Plan",
    t.query_text AS "Query Text"
FROM ranked AS r
{server_join('r.server_id')}
LEFT JOIN LATERAL (
    SELECT query_text
    FROM {collector('query_stats')}
    WHERE server_id = r.server_id
      AND query_hash = r.query_hash
      AND database_name = r.database_name
      AND query_text IS NOT NULL
    ORDER BY collection_time DESC
    LIMIT 1
) AS t ON TRUE
LEFT JOIN module AS m ON m.server_id = r.server_id AND m.sql_handle = r.sql_handle
WHERE t.query_text IS NULL OR t.query_text NOT LIKE 'WAITFOR%'
ORDER BY r.total_elapsed_us DESC
LIMIT ${{topn}}
"""

# CPU-by-database summary - the static substitute for the grid's client-side bar-card roll-up
# (QueriesBarCells.cs RefreshByDbCards). Tiered since it only needs worker_time, CAGG-covered.
_CPU_BY_DATABASE_RAW_SQL = f"""
SELECT database_name AS "Database", SUM(delta_worker_time) / 1000.0 AS "CPU (ms)"
FROM {collector('query_stats')}
WHERE $__timeFilter(collection_time)
  AND {server_filter()}
  AND {multi_filter('database_name', 'database')}
GROUP BY database_name
"""
_CPU_BY_DATABASE_HOURLY_SQL = f"""
SELECT database_name AS "Database", SUM(worker_time_sum) / 1000.0 AS "CPU (ms)"
FROM {rollup('query_stats', 'hourly')}
WHERE $__timeFilter({CAGG_TIME_COL})
  AND {server_filter()}
  AND {multi_filter('database_name', 'database')}
GROUP BY database_name
"""
_CPU_BY_DATABASE_DAILY_SQL = f"""
SELECT database_name AS "Database", SUM(worker_time_sum) / 1000.0 AS "CPU (ms)"
FROM {rollup('query_stats', 'daily')}
WHERE $__timeFilter({CAGG_TIME_COL})
  AND {server_filter()}
  AND {multi_filter('database_name', 'database')}
GROUP BY database_name
"""
_CPU_BY_DATABASE_SQL = (
    tiered(
        {
            "raw": _CPU_BY_DATABASE_RAW_SQL,
            "hourly": _CPU_BY_DATABASE_HOURLY_SQL,
            "daily": _CPU_BY_DATABASE_DAILY_SQL,
        },
        base="query_stats",
    )
    + '\nORDER BY "CPU (ms)" DESC\nLIMIT 15'
)

# Upstream ref: QueryStatsComparisonSql (ViewerDataService.QueryStats.cs).
_TOP_QUERIES_COMPARISON_SQL = f"""
WITH {_COMPARISON_WINDOW_CTE},
top_current AS (
    SELECT server_id, query_hash, database_name
    FROM {collector('query_stats')}, _window
    WHERE {server_filter()}
      AND collection_time >= _window.cur_start AND collection_time <= _window.cur_end
      AND {multi_filter('database_name', 'database')}
      AND delta_execution_count > 0
    GROUP BY server_id, query_hash, database_name
    ORDER BY SUM(delta_execution_count) DESC
    LIMIT 100
),
top_baseline AS (
    SELECT server_id, query_hash, database_name
    FROM {collector('query_stats')}, _window
    WHERE {server_filter()}
      AND collection_time >= _window.base_start AND collection_time <= _window.base_end
      AND {multi_filter('database_name', 'database')}
      AND delta_execution_count > 0
    GROUP BY server_id, query_hash, database_name
    ORDER BY SUM(delta_execution_count) DESC
    LIMIT 100
),
top_hashes AS (
    SELECT DISTINCT server_id, query_hash, database_name
    FROM (SELECT * FROM top_current UNION ALL SELECT * FROM top_baseline) AS combined
),
current_period AS (
    SELECT th.server_id, th.database_name, th.query_hash,
        SUM(qs.delta_execution_count) AS exec_count,
        SUM(qs.delta_elapsed_time)::double precision / NULLIF(SUM(qs.delta_execution_count), 0) / 1000.0 AS avg_duration_ms,
        SUM(qs.delta_worker_time)::double precision / NULLIF(SUM(qs.delta_execution_count), 0) / 1000.0 AS avg_cpu_ms,
        SUM(qs.delta_physical_reads)::double precision / NULLIF(SUM(qs.delta_execution_count), 0) AS avg_reads,
        MAX(qs.query_text) AS query_text
    FROM top_hashes th
    JOIN {collector('query_stats')} qs
      ON qs.query_hash IS NOT DISTINCT FROM th.query_hash
     AND qs.database_name IS NOT DISTINCT FROM th.database_name
     AND qs.server_id = th.server_id
    CROSS JOIN _window
    WHERE qs.collection_time >= _window.cur_start AND qs.collection_time <= _window.cur_end
      AND qs.delta_execution_count > 0
    GROUP BY th.server_id, th.database_name, th.query_hash
),
baseline_period AS (
    SELECT th.server_id, th.database_name, th.query_hash,
        SUM(qs.delta_execution_count) AS exec_count,
        SUM(qs.delta_elapsed_time)::double precision / NULLIF(SUM(qs.delta_execution_count), 0) / 1000.0 AS avg_duration_ms,
        SUM(qs.delta_worker_time)::double precision / NULLIF(SUM(qs.delta_execution_count), 0) / 1000.0 AS avg_cpu_ms,
        SUM(qs.delta_physical_reads)::double precision / NULLIF(SUM(qs.delta_execution_count), 0) AS avg_reads,
        MAX(qs.query_text) AS query_text
    FROM top_hashes th
    JOIN {collector('query_stats')} qs
      ON qs.query_hash IS NOT DISTINCT FROM th.query_hash
     AND qs.database_name IS NOT DISTINCT FROM th.database_name
     AND qs.server_id = th.server_id
    CROSS JOIN _window
    WHERE qs.collection_time >= _window.base_start AND qs.collection_time <= _window.base_end
      AND qs.delta_execution_count > 0
    GROUP BY th.server_id, th.database_name, th.query_hash
)
SELECT
    srv.name AS "Server",
    COALESCE(c.database_name, b.database_name) AS "Database",
    CASE WHEN c.query_hash IS NULL THEN 'GONE' WHEN b.query_hash IS NULL THEN 'NEW' ELSE '' END AS "Status",
    COALESCE(c.query_hash, b.query_hash) AS "Query Hash",
    c.exec_count AS "Executions",
    b.exec_count AS "Base Executions",
    (c.exec_count - b.exec_count) * 100.0 / NULLIF(b.exec_count, 0) AS "Executions Delta %",
    c.avg_duration_ms AS "Avg Duration (ms)",
    b.avg_duration_ms AS "Base Avg Duration (ms)",
    (c.avg_duration_ms - b.avg_duration_ms) * 100.0 / NULLIF(b.avg_duration_ms, 0) AS "Duration Delta %",
    c.avg_cpu_ms AS "Avg CPU (ms)",
    b.avg_cpu_ms AS "Base Avg CPU (ms)",
    (c.avg_cpu_ms - b.avg_cpu_ms) * 100.0 / NULLIF(b.avg_cpu_ms, 0) AS "CPU Delta %",
    c.avg_reads AS "Avg Reads",
    b.avg_reads AS "Base Avg Reads",
    COALESCE(c.query_text, b.query_text) AS "Query Text"
FROM current_period c
FULL OUTER JOIN baseline_period b
  ON COALESCE(c.server_id, -1) = COALESCE(b.server_id, -1)
 AND COALESCE(c.database_name, '') = COALESCE(b.database_name, '')
 AND COALESCE(c.query_hash, '') = COALESCE(b.query_hash, '')
{server_join('COALESCE(c.server_id, b.server_id)')}
ORDER BY "Duration Delta %" DESC NULLS LAST
"""


# Top Procedures by Duration.
# Upstream ref: TopProceduresSql (ViewerDataService.ProcedureStats.cs).
_TOP_PROCEDURES_SQL = f"""
SELECT
    srv.name AS "Server",
    ps.database_name AS "Database",
    ps.schema_name AS "Schema",
    ps.object_name AS "Procedure",
    ps.object_type AS "Type",
    MAX(ps.cached_time) AS "Cached Time",
    MAX(ps.last_execution_time) AS "Last Execution",
    SUM(ps.delta_execution_count)::bigint AS "Executions",
    SUM(ps.delta_worker_time)::bigint / 1000.0 AS "Total CPU (ms)",
    (SUM(ps.delta_worker_time)::double precision / 1000.0) / NULLIF(SUM(ps.delta_execution_count), 0) AS "Avg CPU (ms)",
    SUM(ps.delta_elapsed_time)::bigint / 1000.0 AS "Total Duration (ms)",
    (SUM(ps.delta_elapsed_time)::double precision / 1000.0) / NULLIF(SUM(ps.delta_execution_count), 0) AS "Avg Duration (ms)",
    SUM(ps.delta_logical_reads)::bigint AS "Total Reads",
    SUM(ps.delta_logical_reads)::double precision / NULLIF(SUM(ps.delta_execution_count), 0) AS "Avg Reads",
    SUM(ps.delta_logical_writes)::bigint AS "Total Writes",
    SUM(ps.delta_physical_reads)::bigint AS "Physical Reads",
    SUM(ps.delta_spills)::bigint AS "Total Spills",
    SUM(ps.delta_spills)::double precision / NULLIF(SUM(ps.delta_execution_count), 0) AS "Avg Spills",
    MIN(ps.min_worker_time) / 1000.0 AS "Min CPU (ms)",
    MAX(ps.max_worker_time) / 1000.0 AS "Max CPU (ms)",
    MIN(ps.min_elapsed_time) / 1000.0 AS "Min Duration (ms)",
    MAX(ps.max_elapsed_time) / 1000.0 AS "Max Duration (ms)",
    MIN(ps.min_logical_reads) AS "Min Reads",
    MAX(ps.max_logical_reads) AS "Max Reads",
    MIN(ps.min_physical_reads) AS "Min Phys Reads",
    MAX(ps.max_physical_reads) AS "Max Phys Reads",
    MIN(ps.min_logical_writes) AS "Min Writes",
    MAX(ps.max_logical_writes) AS "Max Writes",
    MIN(ps.min_spills) AS "Min Spills",
    MAX(ps.max_spills) AS "Max Spills",
    MAX(ps.sql_handle) AS "SQL Handle",
    MAX(ps.plan_handle) AS "Plan Handle",
    bool_or(ps.query_plan_xml IS NOT NULL OR ps.query_plan_digest IS NOT NULL) AS "Has Query Plan"
FROM {collector('procedure_stats')} AS ps
{server_join('ps.server_id')}
WHERE {server_filter('ps.server_id')}
  AND $__timeFilter(ps.collection_time)
  AND {multi_filter('ps.database_name', 'database')}
GROUP BY srv.name, ps.database_name, ps.schema_name, ps.object_name, ps.object_type
HAVING SUM(ps.delta_execution_count) > 0 OR SUM(ps.delta_elapsed_time) > 0
ORDER BY SUM(ps.delta_elapsed_time) DESC
LIMIT ${{topn}}
"""

# Upstream ref: ProcedureStatsComparisonSql (ViewerDataService.ProcedureStats.cs).
_TOP_PROCEDURES_COMPARISON_SQL = f"""
WITH {_COMPARISON_WINDOW_CTE},
top_current AS (
    SELECT server_id, database_name, schema_name, object_name
    FROM {collector('procedure_stats')}, _window
    WHERE {server_filter()}
      AND collection_time >= _window.cur_start AND collection_time <= _window.cur_end
      AND {multi_filter('database_name', 'database')}
      AND delta_execution_count > 0
    GROUP BY server_id, database_name, schema_name, object_name
    ORDER BY SUM(delta_execution_count) DESC
    LIMIT 100
),
top_baseline AS (
    SELECT server_id, database_name, schema_name, object_name
    FROM {collector('procedure_stats')}, _window
    WHERE {server_filter()}
      AND collection_time >= _window.base_start AND collection_time <= _window.base_end
      AND {multi_filter('database_name', 'database')}
      AND delta_execution_count > 0
    GROUP BY server_id, database_name, schema_name, object_name
    ORDER BY SUM(delta_execution_count) DESC
    LIMIT 100
),
top_procs AS (
    SELECT DISTINCT server_id, database_name, schema_name, object_name
    FROM (SELECT * FROM top_current UNION ALL SELECT * FROM top_baseline) AS combined
),
current_period AS (
    SELECT tp.server_id, tp.database_name, tp.schema_name, tp.object_name,
        SUM(ps.delta_execution_count) AS exec_count,
        SUM(ps.delta_elapsed_time)::double precision / NULLIF(SUM(ps.delta_execution_count), 0) / 1000.0 AS avg_duration_ms,
        SUM(ps.delta_worker_time)::double precision / NULLIF(SUM(ps.delta_execution_count), 0) / 1000.0 AS avg_cpu_ms,
        SUM(ps.delta_physical_reads)::double precision / NULLIF(SUM(ps.delta_execution_count), 0) AS avg_reads
    FROM top_procs tp
    JOIN {collector('procedure_stats')} ps
      ON ps.database_name IS NOT DISTINCT FROM tp.database_name
     AND ps.schema_name IS NOT DISTINCT FROM tp.schema_name
     AND ps.object_name IS NOT DISTINCT FROM tp.object_name
     AND ps.server_id = tp.server_id
    CROSS JOIN _window
    WHERE ps.collection_time >= _window.cur_start AND ps.collection_time <= _window.cur_end
      AND ps.delta_execution_count > 0
    GROUP BY tp.server_id, tp.database_name, tp.schema_name, tp.object_name
),
baseline_period AS (
    SELECT tp.server_id, tp.database_name, tp.schema_name, tp.object_name,
        SUM(ps.delta_execution_count) AS exec_count,
        SUM(ps.delta_elapsed_time)::double precision / NULLIF(SUM(ps.delta_execution_count), 0) / 1000.0 AS avg_duration_ms,
        SUM(ps.delta_worker_time)::double precision / NULLIF(SUM(ps.delta_execution_count), 0) / 1000.0 AS avg_cpu_ms,
        SUM(ps.delta_physical_reads)::double precision / NULLIF(SUM(ps.delta_execution_count), 0) AS avg_reads
    FROM top_procs tp
    JOIN {collector('procedure_stats')} ps
      ON ps.database_name IS NOT DISTINCT FROM tp.database_name
     AND ps.schema_name IS NOT DISTINCT FROM tp.schema_name
     AND ps.object_name IS NOT DISTINCT FROM tp.object_name
     AND ps.server_id = tp.server_id
    CROSS JOIN _window
    WHERE ps.collection_time >= _window.base_start AND ps.collection_time <= _window.base_end
      AND ps.delta_execution_count > 0
    GROUP BY tp.server_id, tp.database_name, tp.schema_name, tp.object_name
)
SELECT
    srv.name AS "Server",
    COALESCE(c.database_name, b.database_name) AS "Database",
    CASE WHEN c.object_name IS NULL THEN 'GONE' WHEN b.object_name IS NULL THEN 'NEW' ELSE '' END AS "Status",
    COALESCE(c.schema_name, b.schema_name) || '.' || COALESCE(c.object_name, b.object_name) AS "Procedure",
    c.exec_count AS "Executions",
    b.exec_count AS "Base Executions",
    (c.exec_count - b.exec_count) * 100.0 / NULLIF(b.exec_count, 0) AS "Executions Delta %",
    c.avg_duration_ms AS "Avg Duration (ms)",
    b.avg_duration_ms AS "Base Avg Duration (ms)",
    (c.avg_duration_ms - b.avg_duration_ms) * 100.0 / NULLIF(b.avg_duration_ms, 0) AS "Duration Delta %",
    c.avg_cpu_ms AS "Avg CPU (ms)",
    b.avg_cpu_ms AS "Base Avg CPU (ms)",
    (c.avg_cpu_ms - b.avg_cpu_ms) * 100.0 / NULLIF(b.avg_cpu_ms, 0) AS "CPU Delta %",
    c.avg_reads AS "Avg Reads",
    b.avg_reads AS "Base Avg Reads"
FROM current_period c
FULL OUTER JOIN baseline_period b
  ON COALESCE(c.server_id, -1) = COALESCE(b.server_id, -1)
 AND COALESCE(c.database_name, '') = COALESCE(b.database_name, '')
 AND COALESCE(c.schema_name, '') = COALESCE(b.schema_name, '')
 AND COALESCE(c.object_name, '') = COALESCE(b.object_name, '')
{server_join('COALESCE(c.server_id, b.server_id)')}
ORDER BY "Duration Delta %" DESC NULLS LAST
"""


# Query Store by Duration.
# Upstream ref: QueryStoreTopSql (ViewerDataService.QueryStore.cs). replica_role is a GROUP BY
# key, not aggregated away - see that file's header comment on shared/centralized Query Store
# for AGs on 2022+ (grouping it out would blend primary and secondary workload into one row).
_QUERY_STORE_SQL = f"""
WITH ranked AS (
    SELECT
        qsd.server_id,
        qsd.database_name,
        qsd.query_id,
        qsd.plan_id,
        qsd.query_hash,
        qsd.replica_role,
        MAX(qsd.module_name) AS module_name,
        SUM(qsd.execution_count)::bigint AS total_executions,
        AVG(qsd.avg_duration_us::double precision) / 1000.0 AS avg_duration_ms,
        AVG(qsd.avg_cpu_time_us::double precision) / 1000.0 AS avg_cpu_time_ms,
        AVG(qsd.avg_logical_io_reads::double precision) AS avg_logical_reads,
        AVG(qsd.avg_logical_io_writes::double precision) AS avg_logical_writes,
        AVG(qsd.avg_physical_io_reads::double precision) AS avg_physical_reads,
        AVG(qsd.avg_rowcount::double precision) AS avg_rowcount,
        MIN(qsd.min_dop) AS min_dop,
        MAX(qsd.max_dop) AS max_dop,
        MAX(qsd.last_execution_time) AS last_execution_time,
        MAX(qsd.query_plan_hash) AS query_plan_hash,
        bool_or(qsd.is_forced_plan) AS is_forced_plan,
        MAX(qsd.plan_forcing_type) AS plan_forcing_type,
        MAX(qsd.execution_type_desc) AS execution_type_desc,
        MIN(qsd.first_execution_time) AS first_execution_time,
        AVG(qsd.avg_clr_time_us::double precision) / 1000.0 AS avg_clr_time_ms,
        AVG(qsd.avg_tempdb_space_used::double precision) AS avg_tempdb_space_used,
        AVG(qsd.avg_log_bytes_used::double precision) AS avg_log_bytes_used,
        MAX(qsd.plan_type) AS plan_type,
        SUM(qsd.force_failure_count)::bigint AS force_failure_count,
        MAX(qsd.last_force_failure_reason) AS last_force_failure_reason,
        MAX(qsd.compatibility_level) AS compatibility_level,
        MIN(qsd.min_duration_us::double precision) / 1000.0 AS min_duration_ms,
        MAX(qsd.max_duration_us::double precision) / 1000.0 AS max_duration_ms,
        MIN(qsd.min_cpu_time_us::double precision) / 1000.0 AS min_cpu_time_ms,
        MAX(qsd.max_cpu_time_us::double precision) / 1000.0 AS max_cpu_time_ms,
        MIN(qsd.min_logical_io_reads::double precision) AS min_logical_reads,
        MAX(qsd.max_logical_io_reads::double precision) AS max_logical_reads,
        MIN(qsd.min_logical_io_writes::double precision) AS min_logical_writes,
        MAX(qsd.max_logical_io_writes::double precision) AS max_logical_writes,
        MIN(qsd.min_physical_io_reads::double precision) AS min_physical_reads,
        MAX(qsd.max_physical_io_reads::double precision) AS max_physical_reads,
        MIN(qsd.min_clr_time_us::double precision) / 1000.0 AS min_clr_time_ms,
        MAX(qsd.max_clr_time_us::double precision) / 1000.0 AS max_clr_time_ms,
        MIN(qsd.min_rowcount::double precision) AS min_rowcount,
        MAX(qsd.max_rowcount::double precision) AS max_rowcount,
        MIN(qsd.min_log_bytes_used::double precision) AS min_log_bytes_used,
        MAX(qsd.max_log_bytes_used::double precision) AS max_log_bytes_used,
        MIN(qsd.min_tempdb_space_used::double precision) AS min_tempdb_space_used,
        MAX(qsd.max_tempdb_space_used::double precision) AS max_tempdb_space_used,
        AVG(qsd.avg_query_max_used_memory::double precision) * 8.0 / 1024.0 AS avg_memory_mb,
        MIN(qsd.min_query_max_used_memory::double precision) * 8.0 / 1024.0 AS min_memory_mb,
        MAX(qsd.max_query_max_used_memory::double precision) * 8.0 / 1024.0 AS max_memory_mb,
        AVG(qsd.avg_num_physical_io_reads::double precision) AS avg_num_physical_io_reads,
        MIN(qsd.min_num_physical_io_reads::double precision) AS min_num_physical_io_reads,
        MAX(qsd.max_num_physical_io_reads::double precision) AS max_num_physical_io_reads
    FROM {collector('query_store_stats')} AS qsd
    WHERE {server_filter('qsd.server_id')}
      AND $__timeFilter(qsd.collection_time)
      AND {multi_filter('qsd.database_name', 'database')}
    GROUP BY qsd.server_id, qsd.database_name, qsd.query_id, qsd.plan_id, qsd.query_hash, qsd.replica_role
    ORDER BY SUM(qsd.execution_count) * AVG(qsd.avg_duration_us::double precision) DESC
    LIMIT ${{topn}} + 5
)
SELECT
    srv.name AS "Server",
    r.database_name AS "Database",
    r.query_id AS "Query ID",
    r.plan_id AS "Plan ID",
    r.replica_role AS "Replica",
    r.module_name AS "Module",
    r.first_execution_time AS "First Execution",
    r.last_execution_time AS "Last Execution",
    r.total_executions AS "Executions",
    r.total_executions * r.avg_duration_ms AS "Total Duration (ms)",
    r.avg_duration_ms AS "Avg Duration (ms)",
    r.min_duration_ms AS "Min Duration (ms)",
    r.max_duration_ms AS "Max Duration (ms)",
    r.total_executions * r.avg_cpu_time_ms AS "Total CPU (ms)",
    r.avg_cpu_time_ms AS "Avg CPU (ms)",
    r.min_cpu_time_ms AS "Min CPU (ms)",
    r.max_cpu_time_ms AS "Max CPU (ms)",
    r.avg_logical_reads AS "Avg Reads",
    r.min_logical_reads AS "Min Reads",
    r.max_logical_reads AS "Max Reads",
    r.avg_logical_writes AS "Avg Writes",
    r.min_logical_writes AS "Min Writes",
    r.max_logical_writes AS "Max Writes",
    r.avg_physical_reads AS "Avg Phys Reads",
    r.min_physical_reads AS "Min Phys Reads",
    r.max_physical_reads AS "Max Phys Reads",
    r.avg_rowcount AS "Avg Rows",
    r.min_rowcount AS "Min Rows",
    r.max_rowcount AS "Max Rows",
    r.min_dop AS "Min DOP",
    r.max_dop AS "Max DOP",
    r.avg_memory_mb AS "Avg Mem (MB)",
    r.min_memory_mb AS "Min Mem (MB)",
    r.max_memory_mb AS "Max Mem (MB)",
    r.avg_clr_time_ms AS "Avg CLR (ms)",
    r.min_clr_time_ms AS "Min CLR (ms)",
    r.max_clr_time_ms AS "Max CLR (ms)",
    r.avg_num_physical_io_reads AS "Avg Phys IO Reads",
    r.min_num_physical_io_reads AS "Min Phys IO Reads",
    r.max_num_physical_io_reads AS "Max Phys IO Reads",
    r.avg_tempdb_space_used AS "Avg tempdb Pages",
    r.min_tempdb_space_used AS "Min tempdb Pages",
    r.max_tempdb_space_used AS "Max tempdb Pages",
    r.avg_log_bytes_used AS "Avg Log Bytes",
    r.min_log_bytes_used AS "Min Log Bytes",
    r.max_log_bytes_used AS "Max Log Bytes",
    r.is_forced_plan AS "Forced",
    r.plan_forcing_type AS "Force Type",
    r.execution_type_desc AS "Exec Type",
    r.plan_type AS "Plan Type",
    r.force_failure_count AS "Force Failures",
    r.last_force_failure_reason AS "Force Failure Reason",
    r.compatibility_level AS "Compat",
    r.query_hash AS "Query Hash",
    r.query_plan_hash AS "Plan Hash",
    t.query_text AS "Query Text"
FROM ranked AS r
{server_join('r.server_id')}
LEFT JOIN LATERAL (
    SELECT query_text
    FROM {collector('query_store_stats')}
    WHERE server_id = r.server_id
      AND query_id = r.query_id
      AND database_name = r.database_name
      AND query_text IS NOT NULL
    ORDER BY collection_time DESC
    LIMIT 1
) AS t ON TRUE
WHERE t.query_text IS NULL OR t.query_text NOT LIKE 'WAITFOR%'
ORDER BY r.total_executions * r.avg_duration_ms DESC
LIMIT ${{topn}}
"""

# Upstream ref: QueryStoreComparisonSql (ViewerDataService.QueryStore.cs) - execution-count
# weighted averages, since Query Store rows are already per-interval averages.
_QUERY_STORE_COMPARISON_SQL = f"""
WITH {_COMPARISON_WINDOW_CTE},
top_current AS (
    SELECT server_id, database_name, query_hash
    FROM {collector('query_store_stats')}, _window
    WHERE {server_filter()}
      AND collection_time >= _window.cur_start AND collection_time <= _window.cur_end
      AND {multi_filter('database_name', 'database')}
      AND execution_count > 0
    GROUP BY server_id, database_name, query_hash
    ORDER BY SUM(execution_count) DESC
    LIMIT 100
),
top_baseline AS (
    SELECT server_id, database_name, query_hash
    FROM {collector('query_store_stats')}, _window
    WHERE {server_filter()}
      AND collection_time >= _window.base_start AND collection_time <= _window.base_end
      AND {multi_filter('database_name', 'database')}
      AND execution_count > 0
    GROUP BY server_id, database_name, query_hash
    ORDER BY SUM(execution_count) DESC
    LIMIT 100
),
top_hashes AS (
    SELECT DISTINCT server_id, database_name, query_hash
    FROM (SELECT * FROM top_current UNION ALL SELECT * FROM top_baseline) AS combined
),
current_period AS (
    SELECT th.server_id, th.database_name, th.query_hash,
        SUM(qs.execution_count) AS exec_count,
        SUM(qs.execution_count * qs.avg_duration_us::double precision) / NULLIF(SUM(qs.execution_count), 0) / 1000.0 AS avg_duration_ms,
        SUM(qs.execution_count * qs.avg_cpu_time_us::double precision) / NULLIF(SUM(qs.execution_count), 0) / 1000.0 AS avg_cpu_ms,
        SUM(qs.execution_count * qs.avg_logical_io_reads::double precision) / NULLIF(SUM(qs.execution_count), 0) AS avg_reads,
        MAX(qs.query_text) AS query_text
    FROM top_hashes th
    JOIN {collector('query_store_stats')} qs
      ON qs.query_hash IS NOT DISTINCT FROM th.query_hash
     AND qs.database_name IS NOT DISTINCT FROM th.database_name
     AND qs.server_id = th.server_id
    CROSS JOIN _window
    WHERE qs.collection_time >= _window.cur_start AND qs.collection_time <= _window.cur_end
      AND qs.execution_count > 0
    GROUP BY th.server_id, th.database_name, th.query_hash
),
baseline_period AS (
    SELECT th.server_id, th.database_name, th.query_hash,
        SUM(qs.execution_count) AS exec_count,
        SUM(qs.execution_count * qs.avg_duration_us::double precision) / NULLIF(SUM(qs.execution_count), 0) / 1000.0 AS avg_duration_ms,
        SUM(qs.execution_count * qs.avg_cpu_time_us::double precision) / NULLIF(SUM(qs.execution_count), 0) / 1000.0 AS avg_cpu_ms,
        SUM(qs.execution_count * qs.avg_logical_io_reads::double precision) / NULLIF(SUM(qs.execution_count), 0) AS avg_reads,
        MAX(qs.query_text) AS query_text
    FROM top_hashes th
    JOIN {collector('query_store_stats')} qs
      ON qs.query_hash IS NOT DISTINCT FROM th.query_hash
     AND qs.database_name IS NOT DISTINCT FROM th.database_name
     AND qs.server_id = th.server_id
    CROSS JOIN _window
    WHERE qs.collection_time >= _window.base_start AND qs.collection_time <= _window.base_end
      AND qs.execution_count > 0
    GROUP BY th.server_id, th.database_name, th.query_hash
)
SELECT
    srv.name AS "Server",
    COALESCE(c.database_name, b.database_name) AS "Database",
    CASE WHEN c.query_hash IS NULL THEN 'GONE' WHEN b.query_hash IS NULL THEN 'NEW' ELSE '' END AS "Status",
    COALESCE(c.query_hash, b.query_hash) AS "Query Hash",
    c.exec_count AS "Executions",
    b.exec_count AS "Base Executions",
    (c.exec_count - b.exec_count) * 100.0 / NULLIF(b.exec_count, 0) AS "Executions Delta %",
    c.avg_duration_ms AS "Avg Duration (ms)",
    b.avg_duration_ms AS "Base Avg Duration (ms)",
    (c.avg_duration_ms - b.avg_duration_ms) * 100.0 / NULLIF(b.avg_duration_ms, 0) AS "Duration Delta %",
    c.avg_cpu_ms AS "Avg CPU (ms)",
    b.avg_cpu_ms AS "Base Avg CPU (ms)",
    (c.avg_cpu_ms - b.avg_cpu_ms) * 100.0 / NULLIF(b.avg_cpu_ms, 0) AS "CPU Delta %",
    c.avg_reads AS "Avg Reads",
    b.avg_reads AS "Base Avg Reads",
    COALESCE(c.query_text, b.query_text) AS "Query Text"
FROM current_period c
FULL OUTER JOIN baseline_period b
  ON COALESCE(c.server_id, -1) = COALESCE(b.server_id, -1)
 AND COALESCE(c.database_name, '') = COALESCE(b.database_name, '')
 AND COALESCE(c.query_hash, '') = COALESCE(b.query_hash, '')
{server_join('COALESCE(c.server_id, b.server_id)')}
ORDER BY "Duration Delta %" DESC NULLS LAST
"""


# Query Store Regressions.
# Upstream ref: QueryStoreRegressionsSql (ViewerDataService.QueryStoreRegressions.cs) - the
# Postgres port of the Dashboard's report.query_store_regressions TVF. Baseline is EVERY capture
# before the window start (unbounded lookback, not tiered - see module docstring); recent is the
# dashboard's own window. Filter gate is CPU-only (> 25% CPU regression), ranked by
# execution-count-weighted extra duration, matching the upstream TVF's actual (not its stale
# doc-comment's claimed) behavior.
_QUERY_STORE_REGRESSIONS_SQL = f"""
WITH baseline_performance AS (
    SELECT
        server_id, database_name, query_id,
        AVG(avg_duration_us::double precision) / 1000.0 AS avg_duration_ms,
        AVG(avg_cpu_time_us::double precision) / 1000.0 AS avg_cpu_time_ms,
        AVG(avg_logical_io_reads::double precision) AS avg_logical_io_reads,
        SUM(execution_count)::bigint AS exec_count,
        COUNT(DISTINCT plan_id)::int AS plan_count
    FROM {collector('query_store_stats')}
    WHERE {server_filter()}
      AND collection_time < $__timeFrom()
      AND {multi_filter('database_name', 'database')}
    GROUP BY server_id, database_name, query_id
),
recent_performance AS (
    SELECT
        server_id, database_name, query_id,
        MAX(query_text) AS query_text_sample,
        AVG(avg_duration_us::double precision) / 1000.0 AS avg_duration_ms,
        AVG(avg_cpu_time_us::double precision) / 1000.0 AS avg_cpu_time_ms,
        AVG(avg_logical_io_reads::double precision) AS avg_logical_io_reads,
        SUM(execution_count)::bigint AS exec_count,
        COUNT(DISTINCT plan_id)::int AS plan_count,
        MAX(last_execution_time) AS last_execution_time
    FROM {collector('query_store_stats')}
    WHERE {server_filter()}
      AND $__timeFilter(collection_time)
      AND {multi_filter('database_name', 'database')}
    GROUP BY server_id, database_name, query_id
)
SELECT
    srv.name AS "Server",
    r.database_name AS "Database",
    r.query_id AS "Query ID",
    b.avg_duration_ms AS "Baseline Duration (ms)",
    r.avg_duration_ms AS "Recent Duration (ms)",
    (r.avg_duration_ms - b.avg_duration_ms) * 100.0 / NULLIF(b.avg_duration_ms, 0) AS "Duration Regression %",
    b.avg_cpu_time_ms AS "Baseline CPU (ms)",
    r.avg_cpu_time_ms AS "Recent CPU (ms)",
    (r.avg_cpu_time_ms - b.avg_cpu_time_ms) * 100.0 / NULLIF(b.avg_cpu_time_ms, 0) AS "CPU Regression %",
    b.avg_logical_io_reads AS "Baseline Reads",
    r.avg_logical_io_reads AS "Recent Reads",
    (r.avg_logical_io_reads - b.avg_logical_io_reads) * 100.0 / NULLIF(b.avg_logical_io_reads, 0) AS "IO Regression %",
    (r.avg_duration_ms - b.avg_duration_ms) * r.exec_count AS "Total Impact (ms)",
    b.exec_count AS "Base Execs",
    r.exec_count AS "Recent Execs",
    b.plan_count AS "Base Plans",
    r.plan_count AS "Recent Plans",
    CASE
        WHEN (r.avg_duration_ms - b.avg_duration_ms) * 100.0 / NULLIF(b.avg_duration_ms, 0) > 100 THEN 'CRITICAL'
        WHEN (r.avg_duration_ms - b.avg_duration_ms) * 100.0 / NULLIF(b.avg_duration_ms, 0) > 50 THEN 'HIGH'
        WHEN (r.avg_duration_ms - b.avg_duration_ms) * 100.0 / NULLIF(b.avg_duration_ms, 0) > 25 THEN 'MEDIUM'
        ELSE 'LOW'
    END AS "Severity",
    r.query_text_sample AS "Query Text",
    r.last_execution_time AS "Last Execution"
FROM recent_performance AS r
JOIN baseline_performance AS b
  ON b.server_id = r.server_id AND b.database_name = r.database_name AND b.query_id = r.query_id
{server_join('r.server_id')}
WHERE (r.avg_cpu_time_ms - b.avg_cpu_time_ms) * 100.0 / NULLIF(b.avg_cpu_time_ms, 0) > 25
ORDER BY "Total Impact (ms)" DESC
LIMIT 50
"""


# Query Heatmap.
# Upstream ref: BuildQueryHeatmapSql / HeatmapMetricExpr (ViewerDataService.QueryHeatmap.cs).
# Reads v_query_stats at row granularity (per-query-execution magnitude), which the hourly/daily
# CAGGs cannot answer (they collapse per-row execution counts into bucket sums) - raw-only,
# bounded by raw retention, matching the module docstring's tiering note.
_HEATMAP_METRIC_EXPR = """CASE ${heatmap_metric:sqlstring}
        WHEN 'Duration' THEN (delta_elapsed_time / 1000.0) / NULLIF(delta_execution_count, 0)
        WHEN 'CPU' THEN (delta_worker_time / 1000.0) / NULLIF(delta_execution_count, 0)
        WHEN 'Logical Reads' THEN delta_logical_reads::double precision / NULLIF(delta_execution_count, 0)
        WHEN 'Logical Writes' THEN delta_logical_writes::double precision / NULLIF(delta_execution_count, 0)
        WHEN 'Execution Count' THEN delta_execution_count::double precision
        ELSE (delta_elapsed_time / 1000.0) / NULLIF(delta_execution_count, 0)
    END"""


def _heatmap_bucket_label(bucket_col):
    return f"""CASE {bucket_col}
        WHEN 0 THEN CASE WHEN ${{heatmap_metric:sqlstring}} IN ('Duration', 'CPU') THEN '0: 0-1ms' ELSE '0: 0-1' END
        WHEN 1 THEN CASE WHEN ${{heatmap_metric:sqlstring}} IN ('Duration', 'CPU') THEN '1: 1-10ms' ELSE '1: 1-10' END
        WHEN 2 THEN CASE WHEN ${{heatmap_metric:sqlstring}} IN ('Duration', 'CPU') THEN '2: 10-100ms' ELSE '2: 10-100' END
        WHEN 3 THEN CASE WHEN ${{heatmap_metric:sqlstring}} IN ('Duration', 'CPU') THEN '3: 100ms-1s' ELSE '3: 100-1K' END
        WHEN 4 THEN CASE WHEN ${{heatmap_metric:sqlstring}} IN ('Duration', 'CPU') THEN '4: 1-10s' ELSE '4: 1K-10K' END
        WHEN 5 THEN CASE WHEN ${{heatmap_metric:sqlstring}} IN ('Duration', 'CPU') THEN '5: 10-100s' ELSE '5: 10K-100K' END
        ELSE CASE WHEN ${{heatmap_metric:sqlstring}} IN ('Duration', 'CPU') THEN '6: >100s' ELSE '6: >100K' END
    END"""


_QUERY_HEATMAP_SQL = f"""
WITH base AS (
    SELECT
        date_bin(INTERVAL '5 minutes', collection_time, TIMESTAMP '1970-01-01 00:00:00') AS time_bin,
        {_HEATMAP_METRIC_EXPR} AS metric_value,
        delta_execution_count
    FROM {collector('query_stats')}
    WHERE {server_filter()}
      AND $__timeFilter(collection_time)
      AND {multi_filter('database_name', 'database')}
      AND delta_execution_count > 0
),
binned AS (
    SELECT
        time_bin,
        CASE
            WHEN metric_value < 1 THEN 0
            WHEN metric_value < 10 THEN 1
            WHEN metric_value < 100 THEN 2
            WHEN metric_value < 1000 THEN 3
            WHEN metric_value < 10000 THEN 4
            WHEN metric_value < 100000 THEN 5
            ELSE 6
        END AS bucket_index
    FROM base
    WHERE metric_value IS NOT NULL
)
SELECT
    time_bin AS time,
    {_heatmap_bucket_label('bucket_index')} AS metric,
    COUNT(*)::float AS value
FROM binned
GROUP BY time_bin, bucket_index
ORDER BY 1, 2
"""

# Companion table: the top query per bucket by total impact (metric_value * exec_count) - the
# workaround for a Grafana heatmap panel not supporting per-cell tooltip metadata.
_QUERY_HEATMAP_COMPANION_SQL = f"""
WITH base AS (
    SELECT
        {_HEATMAP_METRIC_EXPR} AS metric_value,
        query_hash,
        delta_execution_count AS exec_count
    FROM {collector('query_stats')}
    WHERE {server_filter()}
      AND $__timeFilter(collection_time)
      AND {multi_filter('database_name', 'database')}
      AND delta_execution_count > 0
),
bucketed AS (
    SELECT
        CASE
            WHEN metric_value < 1 THEN 0
            WHEN metric_value < 10 THEN 1
            WHEN metric_value < 100 THEN 2
            WHEN metric_value < 1000 THEN 3
            WHEN metric_value < 10000 THEN 4
            WHEN metric_value < 100000 THEN 5
            ELSE 6
        END AS bucket_index,
        query_hash, metric_value, exec_count
    FROM base
    WHERE metric_value IS NOT NULL
),
per_query AS (
    SELECT bucket_index, query_hash,
        SUM(metric_value * exec_count) AS total_impact,
        COUNT(*) AS query_count
    FROM bucketed
    GROUP BY bucket_index, query_hash
),
ranked AS (
    SELECT bucket_index, query_hash,
        ROW_NUMBER() OVER (PARTITION BY bucket_index ORDER BY total_impact DESC, query_hash) AS rn
    FROM per_query
),
bucket_total AS (
    SELECT bucket_index, SUM(query_count) AS total_queries
    FROM per_query
    GROUP BY bucket_index
)
SELECT
    {_heatmap_bucket_label('bt.bucket_index')} AS "Bucket",
    bt.total_queries AS "Count",
    qt.query_preview AS "Top Query"
FROM bucket_total bt
JOIN ranked r ON r.bucket_index = bt.bucket_index AND r.rn = 1
LEFT JOIN LATERAL (
    SELECT LEFT(query_text, 300) AS query_preview
    FROM {collector('query_stats')}
    WHERE {server_filter()}
      AND query_hash = r.query_hash
      AND $__timeFilter(collection_time)
      AND delta_execution_count > 0
    ORDER BY collection_time DESC
    LIMIT 1
) qt ON TRUE
ORDER BY bt.bucket_index DESC
"""


def queries():
    """Build the Queries dashboard."""
    reset_id()
    panels: list[dict] = []

    y = subtab(
        panels,
        "Performance Trends",
        0,
        [
            (
                6,
                8,
                lambda x, y, w, h: timeseries(
                    "Query Duration (elapsed ms/s)",
                    x,
                    y,
                    w,
                    h,
                    [target(f"{_QUERY_DURATION_TREND_SQL}\nORDER BY 1")],
                    unit="ms",
                ),
            ),
            (
                6,
                8,
                lambda x, y, w, h: timeseries(
                    "Procedure Duration (elapsed ms/s)",
                    x,
                    y,
                    w,
                    h,
                    [target(f"{_PROCEDURE_DURATION_TREND_SQL}\nORDER BY 1")],
                    unit="ms",
                ),
            ),
            (
                6,
                8,
                lambda x, y, w, h: timeseries(
                    "Query Store Duration (elapsed ms/s)",
                    x,
                    y,
                    w,
                    h,
                    [target(f"{_QUERY_STORE_DURATION_TREND_SQL}\nORDER BY 1")],
                    unit="ms",
                ),
            ),
            (
                6,
                8,
                lambda x, y, w, h: timeseries(
                    "Execution Count (executions/s)",
                    x,
                    y,
                    w,
                    h,
                    [target(f"{_EXECUTION_COUNT_TREND_SQL}\nORDER BY 1")],
                    unit="ops",
                ),
            ),
        ],
    )

    y = subtab(
        panels,
        "Active Queries",
        y,
        [
            (
                24,
                6,
                lambda x, y, w, h: timeseries(
                    "Active Sessions (hourly)",
                    x,
                    y,
                    w,
                    h,
                    [target(f"{_ACTIVE_QUERIES_SLICER_SQL}\nORDER BY 1")],
                    unit="short",
                ),
            ),
            (
                24,
                14,
                lambda x, y, w, h: table(
                    "Active queries (stored sp_WhoIsActive-style snapshots)",
                    x,
                    y,
                    w,
                    h,
                    _ACTIVE_QUERIES_SQL,
                    sort_by=[{"displayName": "Collected", "desc": True}],
                    description="Historical snapshots from collect.query_snapshots, bound by the "
                    "dashboard time range. Current Active Queries (live DMV query against the "
                    "monitored server) is not portable here - see the module docstring.",
                ),
            ),
        ],
    )

    y = subtab(
        panels,
        "Top Queries by Duration",
        y,
        [
            (
                16,
                6,
                lambda x, y, w, h: timeseries(
                    "Total CPU trend (hourly, fixed metric)",
                    x,
                    y,
                    w,
                    h,
                    [target(f"{_TOP_QUERIES_SLICER_SQL}\nORDER BY 1")],
                    unit="ms",
                ),
            ),
            (
                8,
                6,
                lambda x, y, w, h: table(
                    "CPU by database",
                    x,
                    y,
                    w,
                    h,
                    _CPU_BY_DATABASE_SQL,
                    overrides=[col_gauge_bar("CPU (ms)", max_val=None, unit="ms")],
                ),
            ),
        ],
    )
    y = flow(
        panels,
        y,
        [
            (
                24,
                16,
                lambda x, y, w, h: table(
                    "Top queries by duration",
                    x,
                    y,
                    w,
                    h,
                    _TOP_QUERIES_SQL,
                    overrides=[
                        col_gauge_bar("Executions", max_val=None, unit="short"),
                        col_gauge_bar("Total CPU (ms)", max_val=None, unit="ms"),
                        col_gauge_bar("Total Duration (ms)", max_val=None, unit="ms"),
                        col_gauge_bar("Total Reads", max_val=None, unit="short"),
                    ],
                    sort_by=[{"displayName": "Total Duration (ms)", "desc": True}],
                ),
            ),
        ],
    )
    y = flow(
        panels,
        y,
        [
            (
                24,
                10,
                lambda x, y, w, h: table(
                    "Comparison vs baseline ($comparison_baseline)",
                    x,
                    y,
                    w,
                    h,
                    _TOP_QUERIES_COMPARISON_SQL,
                    overrides=[
                        status_colors("Status", {"NEW": "blue", "GONE": "orange"})
                    ],
                    description="Top 100 (by executions) queries in the current window unioned "
                    "with the top 100 in the baseline window, full-outer-joined so NEW/GONE "
                    "queries surface.",
                ),
            ),
        ],
    )

    y = subtab(
        panels,
        "Top Procedures by Duration",
        y,
        [
            (
                24,
                6,
                lambda x, y, w, h: timeseries(
                    "Total CPU trend (hourly, fixed metric)",
                    x,
                    y,
                    w,
                    h,
                    [target(f"{_TOP_PROCEDURES_SLICER_SQL}\nORDER BY 1")],
                    unit="ms",
                ),
            ),
        ],
    )
    y = flow(
        panels,
        y,
        [
            (
                24,
                14,
                lambda x, y, w, h: table(
                    "Top procedures by duration",
                    x,
                    y,
                    w,
                    h,
                    _TOP_PROCEDURES_SQL,
                    sort_by=[{"displayName": "Total Duration (ms)", "desc": True}],
                ),
            ),
        ],
    )
    y = flow(
        panels,
        y,
        [
            (
                24,
                10,
                lambda x, y, w, h: table(
                    "Comparison vs baseline ($comparison_baseline)",
                    x,
                    y,
                    w,
                    h,
                    _TOP_PROCEDURES_COMPARISON_SQL,
                    overrides=[
                        status_colors("Status", {"NEW": "blue", "GONE": "orange"})
                    ],
                ),
            ),
        ],
    )

    y = subtab(
        panels,
        "Query Store by Duration",
        y,
        [
            (
                24,
                6,
                lambda x, y, w, h: timeseries(
                    "Total CPU trend (hourly, fixed metric)",
                    x,
                    y,
                    w,
                    h,
                    [target(f"{_QUERY_STORE_SLICER_SQL}\nORDER BY 1")],
                    unit="ms",
                ),
            ),
        ],
    )
    y = flow(
        panels,
        y,
        [
            (
                24,
                16,
                lambda x, y, w, h: table(
                    "Top queries by duration (Query Store)",
                    x,
                    y,
                    w,
                    h,
                    _QUERY_STORE_SQL,
                    overrides=[
                        status_colors("Forced", {"true": "blue", "false": "text"})
                    ],
                    sort_by=[{"displayName": "Total Duration (ms)", "desc": True}],
                    description="replica_role is a GROUP BY key, not aggregated away, so a "
                    "shared AG Query Store shows one row per replica role instead of blending "
                    "primary and secondary workload.",
                ),
            ),
        ],
    )
    y = flow(
        panels,
        y,
        [
            (
                24,
                10,
                lambda x, y, w, h: table(
                    "Comparison vs baseline ($comparison_baseline)",
                    x,
                    y,
                    w,
                    h,
                    _QUERY_STORE_COMPARISON_SQL,
                    overrides=[
                        status_colors("Status", {"NEW": "blue", "GONE": "orange"})
                    ],
                ),
            ),
        ],
    )

    y = subtab(
        panels,
        "Query Store Regressions",
        y,
        [
            (
                24,
                12,
                lambda x, y, w, h: table(
                    "Query Store regressions (recent vs unbounded baseline)",
                    x,
                    y,
                    w,
                    h,
                    _QUERY_STORE_REGRESSIONS_SQL,
                    overrides=[
                        status_colors(
                            "Severity",
                            {
                                "CRITICAL": "red",
                                "HIGH": "orange",
                                "MEDIUM": "yellow",
                                "LOW": "green",
                            },
                        )
                    ],
                    sort_by=[{"displayName": "Duration Regression %", "desc": True}],
                    description="Baseline is every Query Store capture before the dashboard's "
                    "window start (unbounded lookback, bounded in practice by raw retention); "
                    "recent is the window itself. Only queries with >25% CPU regression are "
                    "included (the upstream TVF's actual gate, not its doc comment's claim).",
                ),
            ),
        ],
    )

    subtab(
        panels,
        "Query Heatmap",
        y,
        [
            (
                16,
                12,
                lambda x, y, w, h: heatmap(
                    "Query heatmap (${heatmap_metric} distribution over time)",
                    x,
                    y,
                    w,
                    h,
                    _QUERY_HEATMAP_SQL,
                    description="Each cell = number of query executions in that metric bucket "
                    "for the 5-minute window. Duration/CPU buckets are in ms; Reads/Writes in "
                    "pages. Y-axis runs 0 (fastest/smallest) to 6 (slowest/largest). Raw-only - "
                    "see the module docstring.",
                ),
            ),
            (
                8,
                12,
                lambda x, y, w, h: table(
                    "Top query per bucket (${heatmap_metric}, by impact)",
                    x,
                    y,
                    w,
                    h,
                    _QUERY_HEATMAP_COMPANION_SQL,
                    description="Top query (by total_impact = metric_value * exec_count) in "
                    "each bucket across the selected time range. Ordered highest bucket first "
                    "to align with the heatmap Y-axis.",
                ),
            ),
        ],
    )

    return dashboard(
        uid("queries"),
        "Query Performance",
        panels,
        [
            server_var(),
            query_var(
                "database",
                "Database",
                _DATABASE_VAR_SQL,
                "Optional database filter, shared across every Queries sub-tab.",
            ),
            custom_var(
                "topn", "Top N", ["25", "10", "50", "100"], "Row cap for the grids."
            ),
            custom_var(
                "comparison_baseline",
                "Comparison Baseline",
                ["Yesterday", "Last Week"],
                "Baseline window offset for the vs-baseline comparison tables.",
            ),
            custom_var(
                "heatmap_metric",
                "Heatmap Metric",
                [
                    "Duration",
                    "CPU",
                    "Logical Reads",
                    "Logical Writes",
                    "Execution Count",
                ],
                "Per-execution metric the Query Heatmap buckets rows by.",
            ),
        ],
    )
