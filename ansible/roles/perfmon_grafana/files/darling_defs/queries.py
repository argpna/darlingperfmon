"""Queries dashboard (Darling line).

Upstream ref: ViewerServerTab.Queries.cs, ViewerDataService.QueryStats.cs, .ProcedureStats.cs,
.QueryStore.cs, .QueryStoreRegressions.cs, .QuerySnapshots.cs, .QueryTrends.cs,
.QueryHeatmap.cs (Darling.Viewer), ViewerServerTab.LongQueries.cs,
ViewerDataService.LongQueries.cs. Eight sub-tabs (seven upstream Queries sub-tabs plus Long
Queries). Current Active Queries (LIVE) is not ported: it needs a live DMV connection to the
monitored server via config_command, and Grafana here talks only to Postgres.
"""

from ._shared import (
    CAGG_TIME_COL,
    col_datalink,
    col_gauge_bar,
    col_hidden,
    col_unit,
    collector,
    custom_var,
    dashboard,
    detail_dashboard,
    fixed,
    flow,
    heatmap,
    logs,
    multi_filter,
    plan_parameters_sql,
    query_var,
    reset_id,
    rollup,
    server_filter,
    server_join,
    server_var,
    SERVER_REGISTRY,
    single_query_var,
    stat,
    stat_grid,
    status_colors,
    subtab,
    table,
    target,
    text_var,
    thresholds,
    tiered,
    timeseries,
    uid,
)

_HOURLY_SECONDS = 3600.0
_DAILY_SECONDS = 86400.0

# Upstream ref: ViewerServerTab.History.cs (WireHistoryDrillDowns)
# Double-click becomes a data link on the identity column; server_id travels as a hidden
# column since $server is multi-select but each history window is single-server.
_QUERY_HISTORY_LINK = col_datalink(
    "Query Hash",
    "View query history",
    "/d/darling-query-stats-history?${__url_time_range}"
    "&var-server=${__data.fields.server_id}"
    "&var-database=${__data.fields.Database}"
    '&var-query_hash=${__data.fields["Query Hash"]}',
)

_PROCEDURE_HISTORY_LINK = col_datalink(
    "Procedure",
    "View procedure history",
    "/d/darling-procedure-history?${__url_time_range}"
    "&var-server=${__data.fields.server_id}"
    "&var-database=${__data.fields.Database}"
    "&var-schema=${__data.fields.Schema}"
    "&var-object_name=${__data.fields.Procedure}",
)

_QUERY_STORE_HISTORY_LINK = col_datalink(
    "Query ID",
    "View Query Store history",
    "/d/darling-query-store-history?${__url_time_range}"
    "&var-server=${__data.fields.server_id}"
    "&var-database=${__data.fields.Database}"
    '&var-query_id=${__data.fields["Query ID"]}'
    '&var-plan_id=${__data.fields["Plan ID"]}',
)

_QUERY_STORE_REGRESSION_HISTORY_LINK = col_datalink(
    "Query ID",
    "View Query Store history",
    "/d/darling-query-store-history?${__url_time_range}"
    "&var-server=${__data.fields.server_id}"
    "&var-database=${__data.fields.Database}"
    '&var-query_id=${__data.fields["Query ID"]}'
    "&var-plan_id=*",
)

_DATABASE_VAR_SQL = f"""
SELECT DISTINCT database_name
FROM {collector('query_stats')}
WHERE {server_filter()}
ORDER BY 1
"""

# Stat row: active query count, top CPU consumer this window, regressions detected.
_ACTIVE_QUERY_COUNT_SQL = f"""
SELECT COUNT(*) AS v
FROM {collector('query_snapshots')} AS qs
WHERE {server_filter('qs.server_id')}
  AND $__timeFilter(qs.collection_time)
  AND {multi_filter('qs.database_name', 'database')}
  AND qs.query_text NOT LIKE 'WAITFOR%'
"""

# Same module-attribution shape as _TOP_QUERIES_SQL below, collapsed to a single scalar.
_TOP_CPU_CONSUMER_SQL = f"""
WITH ranked AS (
    SELECT sql_handle, database_name, SUM(delta_worker_time) AS total_cpu_us
    FROM {collector('query_stats')}
    WHERE {server_filter()}
      AND $__timeFilter(collection_time)
      AND {multi_filter('database_name', 'database')}
    GROUP BY sql_handle, database_name
    ORDER BY total_cpu_us DESC
    LIMIT 1
),
module AS (
    SELECT DISTINCT ON (sql_handle) sql_handle, object_name, schema_name, database_name
    FROM {collector('procedure_stats')}
    WHERE {server_filter()} AND sql_handle IS NOT NULL AND sql_handle <> ''
    ORDER BY sql_handle, collection_time DESC
)
SELECT COALESCE(m.database_name || '.' || m.schema_name || '.' || m.object_name,
                 r.database_name || ' (ad hoc)')
       || ' - ' || {fixed('r.total_cpu_us / 1000.0', 0)} || ' ms' AS v
FROM ranked r
LEFT JOIN module m ON m.sql_handle = r.sql_handle
"""

# Same >25% CPU regression gate as _QUERY_STORE_REGRESSIONS_SQL below, collapsed to a count.
_REGRESSIONS_COUNT_SQL = f"""
WITH baseline_performance AS (
    SELECT server_id, database_name, query_id,
        AVG(avg_cpu_time_us::double precision) / 1000.0 AS avg_cpu_time_ms
    FROM {collector('query_store_stats')}
    WHERE {server_filter()}
      AND collection_time < $__timeFrom()
      AND {multi_filter('database_name', 'database')}
    GROUP BY server_id, database_name, query_id
),
recent_performance AS (
    SELECT server_id, database_name, query_id,
        AVG(avg_cpu_time_us::double precision) / 1000.0 AS avg_cpu_time_ms
    FROM {collector('query_store_stats')}
    WHERE {server_filter()}
      AND $__timeFilter(collection_time)
      AND {multi_filter('database_name', 'database')}
    GROUP BY server_id, database_name, query_id
)
SELECT COUNT(*) AS v
FROM recent_performance AS r
JOIN baseline_performance AS b
  ON b.server_id = r.server_id AND b.database_name = r.database_name AND b.query_id = r.query_id
WHERE (r.avg_cpu_time_ms - b.avg_cpu_time_ms) * 100.0 / NULLIF(b.avg_cpu_time_ms, 0) > 25
"""

_STAT_ROW = [
    {
        "title": "Active Queries",
        "sql": _ACTIVE_QUERY_COUNT_SQL,
        "th": thresholds(("text", None)),
    },
    {
        "title": "Top CPU Consumer This Window",
        "sql": _TOP_CPU_CONSUMER_SQL,
        "th": thresholds(("text", None)),
        "fields": "/.*/",
    },
    {
        "title": "Regressions Detected",
        "sql": _REGRESSIONS_COUNT_SQL,
        "th": thresholds(("green", None), ("red", 1)),
    },
]

# Long Queries section, absorbed from the former standalone dashboard (#1496).
# Upstream ref: GetLongQueryTraceEnabledAsync - per-server override > fleet override > default OFF.
_TRACE_STATUS_SQL = f"""
SELECT
    srv.name AS "Server",
    CASE WHEN COALESCE(
        (SELECT s.enabled FROM config.config_collector_schedules AS s
         WHERE s.collector_name = 'long_query_completions'
           AND s.server_id = srv.server_id),
        (SELECT s.enabled FROM config.config_collector_schedules AS s
         WHERE s.collector_name = 'long_query_completions'
           AND s.server_id IS NULL),
        false
    ) THEN 'ON' ELSE 'OFF' END AS "Trace Status"
FROM {SERVER_REGISTRY} AS srv
WHERE srv.is_enabled
  AND {server_filter('srv.server_id')}
ORDER BY srv.name
"""

# Upstream ref: LongQueryCompletionsSql. IsAborted/IsAttention row tints become column colors below.
_LONG_QUERIES_SQL = f"""
SELECT
    srv.name AS "Server",
    lqc.event_time AS "Event Time",
    lqc.event_type AS "Event Type",
    lqc.duration_microseconds / 1000.0 AS "Duration",
    lqc.cpu_time_microseconds / 1000.0 AS "CPU",
    lqc.logical_reads AS "Logical Reads",
    lqc.physical_reads AS "Physical Reads",
    lqc.writes AS "Writes",
    lqc.row_count AS "Rows",
    lqc.result AS "Result",
    lqc.database_name AS "Database",
    lqc.object_name AS "Object",
    lqc.statement_text AS "Statement",
    lqc.session_id AS "Session",
    lqc.client_app_name AS "Application",
    lqc.server_principal_name AS "Login",
    lqc.query_hash AS "Query Hash"
FROM {collector('long_query_completions')} AS lqc
{server_join('lqc.server_id')}
WHERE $__timeFilter(lqc.collection_time)
  AND {server_filter('lqc.server_id')}
ORDER BY lqc.event_time DESC
LIMIT 200
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
    r.server_id AS "server_id",
    srv.name AS "Server",
    r.database_name AS "Database",
    COALESCE(m.database_name || '.' || m.schema_name || '.' || m.object_name, 'ad hoc') AS "Module",
    r.last_execution_time AS "Last Execution",
    r.creation_time AS "Creation Time",
    r.query_hash AS "Query Hash",
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
    MAX(ps.server_id) AS "server_id",
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
    r.server_id AS "server_id",
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
    r.server_id AS "server_id",
    srv.name AS "Server",
    r.database_name AS "Database",
    CASE
        WHEN (r.avg_duration_ms - b.avg_duration_ms) * 100.0 / NULLIF(b.avg_duration_ms, 0) > 100 THEN 'CRITICAL'
        WHEN (r.avg_duration_ms - b.avg_duration_ms) * 100.0 / NULLIF(b.avg_duration_ms, 0) > 50 THEN 'HIGH'
        WHEN (r.avg_duration_ms - b.avg_duration_ms) * 100.0 / NULLIF(b.avg_duration_ms, 0) > 25 THEN 'MEDIUM'
        ELSE 'LOW'
    END AS "Severity",
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


# Upstream ref: ProcedureHistoryWindow.xaml.cs / QueryStatsHistoryWindow.xaml.cs /
# QueryStoreHistoryWindow.xaml.cs, backed by ViewerDataService.ItemHistory.cs. All three honor
# $__timeFilter (upstream's GetWindowUtc()) - unlike WaitDrillDownWindow's fixed +/-30min.
def _identity_guard(var: str, col: str) -> str:
    """Sentinel-guarded exact-match filter for a single-value identity key, same idiom as
    the optional-filter convention - a cold arrival with no value yet reads as "no filter"
    instead of erroring, same as blocking.py's deadlock_id guard."""
    return (
        f"(${{{var}:sqlstring}} = '*' OR ${{{var}:sqlstring}} = '' "
        f"OR {col} = ${{{var}:sqlstring}})"
    )


_HISTORY_METRIC_OPTIONS = [
    "Avg CPU (ms)",
    "Avg Duration (ms)",
    "Avg Reads",
    "Executions (delta)",
    "CPU (delta ms)",
    "Reads (delta)",
    "Spills (delta)",
]


def _history_metric_expr(prefix: str = "") -> str:
    """CASE mapping shared by procedure/query history charts - both tables use the same
    delta_* column names, so one expression serves both (see _shared.py's Darling delta_
    column note)."""
    p = prefix
    return f"""CASE ${{history_metric:sqlstring}}
        WHEN 'Avg CPU (ms)' THEN ({p}delta_worker_time / 1000.0) / NULLIF({p}delta_execution_count, 0)
        WHEN 'Avg Duration (ms)' THEN ({p}delta_elapsed_time / 1000.0) / NULLIF({p}delta_execution_count, 0)
        WHEN 'Avg Reads' THEN {p}delta_logical_reads::double precision / NULLIF({p}delta_execution_count, 0)
        WHEN 'Executions (delta)' THEN {p}delta_execution_count::double precision
        WHEN 'CPU (delta ms)' THEN {p}delta_worker_time / 1000.0
        WHEN 'Reads (delta)' THEN {p}delta_logical_reads::double precision
        WHEN 'Spills (delta)' THEN {p}delta_spills::double precision
        ELSE ({p}delta_worker_time / 1000.0) / NULLIF({p}delta_execution_count, 0)
    END"""


# Upstream ref: ProcStatsHistorySql (ViewerDataService.ItemHistory.cs)
# procedure_stats has no sample_interval_seconds column, so it's derived via LAG() in-query.
_PROCEDURE_HISTORY_WHERE = f"""
{server_filter('ps.server_id')}
  AND {_identity_guard('database', 'ps.database_name')}
  AND {_identity_guard('schema', 'ps.schema_name')}
  AND {_identity_guard('object_name', 'ps.object_name')}
  AND $__timeFilter(ps.collection_time)
"""

_PROCEDURE_HISTORY_SQL = f"""
SELECT
    ps.collection_time AS "Collection Time",
    ps.last_execution_time AS "Last Execution",
    ps.cached_time AS "Cached Time",
    ps.object_type AS "Object Type",
    ps.delta_execution_count AS "Exec Delta",
    ps.execution_count AS "Total Executions",
    ps.delta_worker_time / 1000.0 AS "CPU Delta (ms)",
    ps.delta_elapsed_time / 1000.0 AS "Duration Delta (ms)",
    (ps.delta_worker_time / 1000.0) / NULLIF(ps.delta_execution_count, 0) AS "Avg CPU (ms)",
    (ps.delta_elapsed_time / 1000.0) / NULLIF(ps.delta_execution_count, 0) AS "Avg Duration (ms)",
    ps.total_worker_time / 1000.0 AS "Total CPU (ms)",
    ps.total_elapsed_time / 1000.0 AS "Total Duration (ms)",
    ps.delta_logical_reads AS "Logical Reads",
    ps.delta_logical_reads::double precision / NULLIF(ps.delta_execution_count, 0) AS "Avg Reads",
    ps.total_logical_reads AS "Total Logical Reads",
    ps.delta_logical_writes AS "Writes",
    ps.delta_logical_writes::double precision / NULLIF(ps.delta_execution_count, 0) AS "Avg Writes",
    ps.total_logical_writes AS "Total Writes",
    ps.delta_physical_reads AS "Physical Reads",
    ps.delta_physical_reads::double precision / NULLIF(ps.delta_execution_count, 0) AS "Avg Phys Reads",
    ps.total_physical_reads AS "Total Phys Reads",
    ps.delta_spills AS "Spills",
    ps.delta_spills::double precision / NULLIF(ps.delta_execution_count, 0) AS "Avg Spills",
    ps.total_spills AS "Total Spills",
    ps.min_worker_time / 1000.0 AS "Min CPU (ms)",
    ps.max_worker_time / 1000.0 AS "Max CPU (ms)",
    ps.min_elapsed_time / 1000.0 AS "Min Duration (ms)",
    ps.max_elapsed_time / 1000.0 AS "Max Duration (ms)",
    ps.min_logical_reads AS "Min Reads",
    ps.max_logical_reads AS "Max Reads",
    ps.min_physical_reads AS "Min Phys Reads",
    ps.max_physical_reads AS "Max Phys Reads",
    ps.min_logical_writes AS "Min Writes",
    ps.max_logical_writes AS "Max Writes",
    ps.min_spills AS "Min Spills",
    ps.max_spills AS "Max Spills",
    CAST(EXTRACT(EPOCH FROM (date_trunc('second', ps.collection_time)
        - date_trunc('second', LAG(ps.collection_time) OVER (ORDER BY ps.collection_time))))
        AS bigint) AS "Interval (sec)",
    ps.sql_handle AS "SQL Handle",
    ps.plan_handle AS "Plan Handle"
FROM {collector('procedure_stats')} AS ps
WHERE {_PROCEDURE_HISTORY_WHERE}
ORDER BY ps.collection_time
"""

# One series (dot cloud) per plan shape, matching query_stats/query_store's grouping -
# plan_handle is procedure_stats' de facto shape id (see the Plan Shapes comment below).
_PROCEDURE_HISTORY_CHART_SQL = f"""
SELECT
    ps.collection_time AS time,
    COALESCE(ps.plan_handle, 'unknown') AS metric,
    {_history_metric_expr('ps.')} AS value
FROM {collector('procedure_stats')} AS ps
WHERE {_PROCEDURE_HISTORY_WHERE}
ORDER BY 1
"""


# Plan Shapes: procedure_stats has no query_plan_hash column (query_stats' plan-cache shape id)
# - a procedure's plan_handle changes on recompile, so it plays that role here instead.
# procedure_stats also has no v_procedure_stats resolving view (see _shared.py's _NO_VIEW), so
# unlike collector('query_stats'), its query_plan_xml can be NULL with the real XML sitting in
# query_plan_dim under query_plan_digest - every read below resolves that join by hand.
_PROCEDURE_PLAN_SHAPE_VAR_SQL = f"""
SELECT
    ps.plan_handle AS __text,
    ps.plan_handle AS __value
FROM {collector('procedure_stats')} AS ps
WHERE {_PROCEDURE_HISTORY_WHERE}
  AND ps.plan_handle IS NOT NULL
GROUP BY ps.plan_handle
ORDER BY MAX(ps.collection_time) DESC
"""

_PROCEDURE_PLAN_SHAPES_SQL = f"""
SELECT
    ps.plan_handle AS "Plan Handle",
    MIN(ps.collection_time) AS "First Seen",
    MAX(ps.collection_time) AS "Last Seen",
    SUM(ps.delta_execution_count)::bigint AS "Executions",
    (SUM(ps.delta_worker_time)::double precision / 1000.0) / NULLIF(SUM(ps.delta_execution_count), 0) AS "Avg CPU (ms)",
    (SUM(ps.delta_elapsed_time)::double precision / 1000.0) / NULLIF(SUM(ps.delta_execution_count), 0) AS "Avg Duration (ms)",
    bool_or(COALESCE(ps.query_plan_xml, dim.query_plan_xml) IS NOT NULL) AS "Has Plan XML"
FROM {collector('procedure_stats')} AS ps
LEFT JOIN collect.query_plan_dim AS dim ON dim.digest = ps.query_plan_digest
WHERE {_PROCEDURE_HISTORY_WHERE}
  AND ps.plan_handle IS NOT NULL
GROUP BY ps.plan_handle
ORDER BY MAX(ps.collection_time) DESC
"""

_PROCEDURE_PLAN_XML_SQL = f"""
SELECT ps.collection_time AS time, COALESCE(ps.query_plan_xml, dim.query_plan_xml) AS "Line"
FROM {collector('procedure_stats')} AS ps
LEFT JOIN collect.query_plan_dim AS dim ON dim.digest = ps.query_plan_digest
WHERE {_PROCEDURE_HISTORY_WHERE}
  AND ps.plan_handle = ${{plan_shape:sqlstring}}
  AND COALESCE(ps.query_plan_xml, dim.query_plan_xml) IS NOT NULL
ORDER BY ps.collection_time DESC
LIMIT 1
"""

_PROCEDURE_PLAN_PARAMETERS_SQL = plan_parameters_sql(f"""
    SELECT COALESCE(ps.query_plan_xml, dim.query_plan_xml) AS plan_xml
    FROM {collector('procedure_stats')} AS ps
    LEFT JOIN collect.query_plan_dim AS dim ON dim.digest = ps.query_plan_digest
    WHERE {_PROCEDURE_HISTORY_WHERE}
      AND ps.plan_handle = ${{plan_shape:sqlstring}}
      AND COALESCE(ps.query_plan_xml, dim.query_plan_xml) IS NOT NULL
    ORDER BY ps.collection_time DESC
    LIMIT 1
""")


# Upstream ref: QueryStatsHistorySql (ViewerDataService.ItemHistory.cs)
_QUERY_STATS_HISTORY_WHERE = f"""
{server_filter('qs.server_id')}
  AND {_identity_guard('database', 'qs.database_name')}
  AND {_identity_guard('query_hash', 'qs.query_hash')}
  AND $__timeFilter(qs.collection_time)
"""

_QUERY_STATS_HISTORY_SQL = f"""
SELECT
    qs.collection_time AS "Collection Time",
    qs.last_execution_time AS "Last Execution",
    qs.creation_time AS "Creation Time",
    qs.delta_execution_count AS "Exec Delta",
    qs.execution_count AS "Total Executions",
    qs.delta_worker_time / 1000.0 AS "CPU Delta (ms)",
    qs.delta_elapsed_time / 1000.0 AS "Duration Delta (ms)",
    (qs.delta_worker_time / 1000.0) / NULLIF(qs.delta_execution_count, 0) AS "Avg CPU (ms)",
    (qs.delta_elapsed_time / 1000.0) / NULLIF(qs.delta_execution_count, 0) AS "Avg Duration (ms)",
    qs.total_worker_time / 1000.0 AS "Total CPU (ms)",
    qs.total_elapsed_time / 1000.0 AS "Total Duration (ms)",
    qs.delta_logical_reads AS "Logical Reads",
    qs.delta_logical_reads::double precision / NULLIF(qs.delta_execution_count, 0) AS "Avg Reads",
    qs.total_logical_reads AS "Total Logical Reads",
    qs.delta_rows AS "Rows",
    qs.delta_rows::double precision / NULLIF(qs.delta_execution_count, 0) AS "Avg Rows",
    qs.total_rows AS "Total Rows",
    qs.delta_logical_writes AS "Writes",
    qs.delta_logical_writes::double precision / NULLIF(qs.delta_execution_count, 0) AS "Avg Writes",
    qs.total_logical_writes AS "Total Writes",
    qs.delta_physical_reads AS "Physical Reads",
    qs.delta_physical_reads::double precision / NULLIF(qs.delta_execution_count, 0) AS "Avg Phys Reads",
    qs.total_physical_reads AS "Total Phys Reads",
    qs.delta_spills AS "Spills",
    qs.total_spills AS "Total Spills",
    qs.min_worker_time / 1000.0 AS "Min CPU (ms)",
    qs.max_worker_time / 1000.0 AS "Max CPU (ms)",
    qs.min_elapsed_time / 1000.0 AS "Min Duration (ms)",
    qs.max_elapsed_time / 1000.0 AS "Max Duration (ms)",
    qs.min_dop AS "Min DOP",
    qs.max_dop AS "Max DOP",
    qs.min_physical_reads AS "Min Phys Reads",
    qs.max_physical_reads AS "Max Phys Reads",
    qs.min_rows AS "Min Rows",
    qs.max_rows AS "Max Rows",
    qs.min_spills AS "Min Spills",
    qs.max_spills AS "Max Spills",
    qs.min_grant_kb AS "Min Grant (KB)",
    qs.max_grant_kb AS "Max Grant (KB)",
    qs.min_used_grant_kb AS "Min Used Grant (KB)",
    qs.max_used_grant_kb AS "Max Used Grant (KB)",
    qs.min_ideal_grant_kb AS "Min Ideal Grant (KB)",
    qs.max_ideal_grant_kb AS "Max Ideal Grant (KB)",
    qs.min_reserved_threads AS "Min Reserved Threads",
    qs.max_reserved_threads AS "Max Reserved Threads",
    qs.min_used_threads AS "Min Used Threads",
    qs.max_used_threads AS "Max Used Threads",
    qs.total_clr_time / 1000.0 AS "Total CLR (ms)",
    qs.sample_interval_seconds AS "Interval (sec)",
    qs.sql_handle AS "SQL Handle",
    qs.plan_handle AS "Plan Handle",
    qs.query_hash AS "Query Hash",
    qs.query_plan_hash AS "Plan Hash",
    qs.query_text AS "Query Text"
FROM {collector('query_stats')} AS qs
WHERE {_QUERY_STATS_HISTORY_WHERE}
ORDER BY qs.collection_time
"""

# One series (dot cloud) per plan shape - see the Plan Shapes comment below for what
# query_plan_hash identifies. Clicking a dot sets the Plan Shape variable.
_QUERY_STATS_HISTORY_CHART_SQL = f"""
SELECT
    qs.collection_time AS time,
    COALESCE(qs.query_plan_hash, 'unknown') AS metric,
    {_history_metric_expr('qs.')} AS value
FROM {collector('query_stats')} AS qs
WHERE {_QUERY_STATS_HISTORY_WHERE}
ORDER BY 1
"""


# Plan Shapes: query_plan_hash is the plan-cache shape identifier (same shape recompiled with
# different sniffed parameters keeps this hash but gets new XML - see PayloadDimensions.cs's
# digest-vs-hash rationale upstream). collector('query_stats') resolves to v_query_stats, which
# already COALESCEs query_plan_xml through the digest-keyed query_plan_dim table, so no extra
# join is needed here.
_QUERY_PLAN_SHAPE_VAR_SQL = f"""
SELECT
    qs.query_plan_hash AS __text,
    qs.query_plan_hash AS __value
FROM {collector('query_stats')} AS qs
WHERE {_QUERY_STATS_HISTORY_WHERE}
  AND qs.query_plan_hash IS NOT NULL
GROUP BY qs.query_plan_hash
ORDER BY MAX(qs.collection_time) DESC
"""

_QUERY_PLAN_SHAPES_SQL = f"""
SELECT
    qs.query_plan_hash AS "Plan Hash",
    MIN(qs.collection_time) AS "First Seen",
    MAX(qs.collection_time) AS "Last Seen",
    SUM(qs.delta_execution_count)::bigint AS "Executions",
    (SUM(qs.delta_worker_time)::double precision / 1000.0) / NULLIF(SUM(qs.delta_execution_count), 0) AS "Avg CPU (ms)",
    (SUM(qs.delta_elapsed_time)::double precision / 1000.0) / NULLIF(SUM(qs.delta_execution_count), 0) AS "Avg Duration (ms)",
    bool_or(qs.query_plan_xml IS NOT NULL) AS "Has Plan XML"
FROM {collector('query_stats')} AS qs
WHERE {_QUERY_STATS_HISTORY_WHERE}
  AND qs.query_plan_hash IS NOT NULL
GROUP BY qs.query_plan_hash
ORDER BY MAX(qs.collection_time) DESC
"""

_QUERY_STATS_PLAN_XML_SQL = f"""
SELECT qs.collection_time AS time, qs.query_plan_xml AS "Line"
FROM {collector('query_stats')} AS qs
WHERE {_QUERY_STATS_HISTORY_WHERE}
  AND qs.query_plan_hash = ${{plan_shape:sqlstring}}
  AND qs.query_plan_xml IS NOT NULL
ORDER BY qs.collection_time DESC
LIMIT 1
"""

_QUERY_STATS_PLAN_PARAMETERS_SQL = plan_parameters_sql(f"""
    SELECT qs.query_plan_xml AS plan_xml
    FROM {collector('query_stats')} AS qs
    WHERE {_QUERY_STATS_HISTORY_WHERE}
      AND qs.query_plan_hash = ${{plan_shape:sqlstring}}
      AND qs.query_plan_xml IS NOT NULL
    ORDER BY qs.collection_time DESC
    LIMIT 1
""")


# Upstream ref: QueryStoreHistorySql (ViewerDataService.ItemHistory.cs) - query-scoped, not
# plan-filtered. plan_id is optional; Query Store Regressions has no single plan_id and
# passes "*".
_QUERY_STORE_HISTORY_WHERE = f"""
{server_filter('qsd.server_id')}
  AND {_identity_guard('database', 'qsd.database_name')}
  AND {_identity_guard('query_id', 'qsd.query_id::text')}
  AND {_identity_guard('plan_id', 'qsd.plan_id::text')}
  AND $__timeFilter(qsd.collection_time)
"""

_QUERY_STORE_HISTORY_SQL = f"""
SELECT
    qsd.collection_time AS "Collection Time",
    qsd.plan_id AS "Plan ID",
    qsd.execution_type_desc AS "Exec Type",
    qsd.first_execution_time AS "First Execution",
    qsd.last_execution_time AS "Last Execution",
    qsd.module_name AS "Module",
    qsd.execution_count AS "Executions",
    qsd.execution_count * qsd.avg_duration_us / 1000.0 AS "Total Duration (ms)",
    qsd.avg_duration_us / 1000.0 AS "Avg Duration (ms)",
    qsd.min_duration_us / 1000.0 AS "Min Duration (ms)",
    qsd.max_duration_us / 1000.0 AS "Max Duration (ms)",
    qsd.execution_count * qsd.avg_cpu_time_us / 1000.0 AS "Total CPU (ms)",
    qsd.avg_cpu_time_us / 1000.0 AS "Avg CPU (ms)",
    qsd.min_cpu_time_us / 1000.0 AS "Min CPU (ms)",
    qsd.max_cpu_time_us / 1000.0 AS "Max CPU (ms)",
    qsd.avg_clr_time_us / 1000.0 AS "Avg CLR (ms)",
    qsd.min_clr_time_us / 1000.0 AS "Min CLR (ms)",
    qsd.max_clr_time_us / 1000.0 AS "Max CLR (ms)",
    qsd.avg_logical_io_reads AS "Avg Reads",
    qsd.min_logical_io_reads AS "Min Reads",
    qsd.max_logical_io_reads AS "Max Reads",
    qsd.avg_logical_io_writes AS "Avg Writes",
    qsd.min_logical_io_writes AS "Min Writes",
    qsd.max_logical_io_writes AS "Max Writes",
    qsd.avg_log_bytes_used / 1048576.0 AS "Avg Log (MB)",
    qsd.min_log_bytes_used / 1048576.0 AS "Min Log (MB)",
    qsd.max_log_bytes_used / 1048576.0 AS "Max Log (MB)",
    qsd.avg_physical_io_reads AS "Avg Phys Reads",
    qsd.min_physical_io_reads AS "Min Phys Reads",
    qsd.max_physical_io_reads AS "Max Phys Reads",
    qsd.avg_num_physical_io_reads AS "Avg Num Phys Reads",
    qsd.min_num_physical_io_reads AS "Min Num Phys Reads",
    qsd.max_num_physical_io_reads AS "Max Num Phys Reads",
    qsd.avg_rowcount AS "Avg Rows",
    qsd.min_rowcount AS "Min Rows",
    qsd.max_rowcount AS "Max Rows",
    qsd.avg_query_max_used_memory * 8.0 / 1024.0 AS "Avg Mem (MB)",
    qsd.min_query_max_used_memory * 8.0 / 1024.0 AS "Min Mem (MB)",
    qsd.max_query_max_used_memory * 8.0 / 1024.0 AS "Max Mem (MB)",
    qsd.avg_tempdb_space_used * 8.0 / 1024.0 AS "Avg tempdb (MB)",
    qsd.min_tempdb_space_used * 8.0 / 1024.0 AS "Min tempdb (MB)",
    qsd.max_tempdb_space_used * 8.0 / 1024.0 AS "Max tempdb (MB)",
    qsd.min_dop AS "Min DOP",
    qsd.max_dop AS "Max DOP",
    qsd.plan_type AS "Plan Type",
    qsd.is_forced_plan AS "Forced",
    qsd.force_failure_count AS "Force Failures",
    qsd.last_force_failure_reason AS "Last Failure Reason",
    qsd.plan_forcing_type AS "Forcing Type",
    qsd.compatibility_level AS "Compat Level",
    qsd.query_hash AS "Query Hash",
    qsd.query_plan_hash AS "Query Plan Hash"
FROM {collector('query_store_stats')} AS qsd
WHERE {_QUERY_STORE_HISTORY_WHERE}
ORDER BY qsd.collection_time
"""

_QUERY_STORE_HISTORY_METRIC_OPTIONS = [
    "Avg CPU (ms)",
    "Avg Duration (ms)",
    "Avg Reads",
    "Avg Rows",
    "Executions",
    "Total CPU (ms)",
    "Total Duration (ms)",
]

_QUERY_STORE_HISTORY_METRIC_EXPR = """CASE ${qs_history_metric:sqlstring}
    WHEN 'Avg CPU (ms)' THEN qsd.avg_cpu_time_us / 1000.0
    WHEN 'Avg Duration (ms)' THEN qsd.avg_duration_us / 1000.0
    WHEN 'Avg Reads' THEN qsd.avg_logical_io_reads::double precision
    WHEN 'Avg Rows' THEN qsd.avg_rowcount::double precision
    WHEN 'Executions' THEN qsd.execution_count::double precision
    WHEN 'Total CPU (ms)' THEN qsd.execution_count * qsd.avg_cpu_time_us / 1000.0
    WHEN 'Total Duration (ms)' THEN qsd.execution_count * qsd.avg_duration_us / 1000.0
    ELSE qsd.avg_duration_us / 1000.0
END"""

# One series (dot cloud) per plan_id, matching UpdateChart's per-plan grouping. Bare
# plan_id (no "Plan " prefix) so a clicked dot's field name is usable directly as the
# Plan Shape variable's value.
_QUERY_STORE_HISTORY_CHART_SQL = f"""
SELECT
    qsd.collection_time AS time,
    qsd.plan_id::text AS metric,
    {_QUERY_STORE_HISTORY_METRIC_EXPR} AS value
FROM {collector('query_store_stats')} AS qsd
WHERE {_QUERY_STORE_HISTORY_WHERE}
ORDER BY 1
"""


# Plan Shapes: unlike query_stats' hash, Query Store's plan_id is a genuine first-class
# distinct-plan-shape id. This picker deliberately ignores the dashboard's own $plan_id
# textbox filter (server/database/query_id only) so it still lists every shape when arrived
# at via the Regressions drill-down, which passes plan_id=* (query-level, no single plan).
_QUERY_STORE_SHAPE_WHERE = f"""
{server_filter('qsd.server_id')}
  AND {_identity_guard('database', 'qsd.database_name')}
  AND {_identity_guard('query_id', 'qsd.query_id::text')}
  AND $__timeFilter(qsd.collection_time)
"""

_QUERY_STORE_PLAN_SHAPE_VAR_SQL = f"""
SELECT
    'Plan ' || qsd.plan_id AS __text,
    qsd.plan_id::text AS __value
FROM {collector('query_store_stats')} AS qsd
WHERE {_QUERY_STORE_SHAPE_WHERE}
GROUP BY qsd.plan_id
ORDER BY MAX(qsd.last_execution_time) DESC
"""

_QUERY_STORE_PLAN_SHAPES_SQL = f"""
SELECT
    qsd.plan_id AS "Plan ID",
    MIN(qsd.first_execution_time) AS "First Execution",
    MAX(qsd.last_execution_time) AS "Last Execution",
    SUM(qsd.execution_count)::bigint AS "Executions",
    AVG(qsd.avg_duration_us::double precision) / 1000.0 AS "Avg Duration (ms)",
    AVG(qsd.avg_cpu_time_us::double precision) / 1000.0 AS "Avg CPU (ms)",
    bool_or(qsd.is_forced_plan) AS "Forced",
    bool_or(qsd.query_plan_text IS NOT NULL) AS "Has Plan XML"
FROM {collector('query_store_stats')} AS qsd
WHERE {_QUERY_STORE_SHAPE_WHERE}
GROUP BY qsd.plan_id
ORDER BY MAX(qsd.last_execution_time) DESC
"""

_QUERY_STORE_PLAN_XML_SQL = f"""
SELECT qsd.collection_time AS time, qsd.query_plan_text AS "Line"
FROM {collector('query_store_stats')} AS qsd
WHERE {_QUERY_STORE_SHAPE_WHERE}
  AND qsd.plan_id::text = ${{plan_shape:sqlstring}}
  AND qsd.query_plan_text IS NOT NULL
ORDER BY qsd.collection_time DESC
LIMIT 1
"""

_QUERY_STORE_PLAN_PARAMETERS_SQL = plan_parameters_sql(f"""
    SELECT qsd.query_plan_text AS plan_xml
    FROM {collector('query_store_stats')} AS qsd
    WHERE {_QUERY_STORE_SHAPE_WHERE}
      AND qsd.plan_id::text = ${{plan_shape:sqlstring}}
      AND qsd.query_plan_text IS NOT NULL
    ORDER BY qsd.collection_time DESC
    LIMIT 1
""")


def queries():
    """Build the Queries dashboard."""
    reset_id()
    panels: list[dict] = []

    y = flow(panels, 0, [(24, 4, stat_grid(_STAT_ROW, cols=3))])

    y = subtab(
        panels,
        "Performance Trends",
        y,
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
                    bars=True,
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
                    "Total CPU trend (hourly)",
                    x,
                    y,
                    w,
                    h,
                    [target(f"{_TOP_QUERIES_SLICER_SQL}\nORDER BY 1")],
                    unit="ms",
                    bars=True,
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
                        col_hidden("server_id"),
                        _QUERY_HISTORY_LINK,
                        col_unit("Executions", "short"),
                        col_unit("Total CPU (ms)", "ms"),
                        col_unit("Total Duration (ms)", "ms"),
                        col_unit("Total Reads", "short"),
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
                        status_colors("Status", {"NEW": "blue", "GONE": "orange"}, cell_type="color-text")
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
                    "Total CPU trend (hourly)",
                    x,
                    y,
                    w,
                    h,
                    [target(f"{_TOP_PROCEDURES_SLICER_SQL}\nORDER BY 1")],
                    unit="ms",
                    bars=True,
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
                    overrides=[col_hidden("server_id"), _PROCEDURE_HISTORY_LINK],
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
                        status_colors("Status", {"NEW": "blue", "GONE": "orange"}, cell_type="color-text")
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
                    "Total CPU trend (hourly)",
                    x,
                    y,
                    w,
                    h,
                    [target(f"{_QUERY_STORE_SLICER_SQL}\nORDER BY 1")],
                    unit="ms",
                    bars=True,
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
                        col_hidden("server_id"),
                        _QUERY_STORE_HISTORY_LINK,
                        status_colors("Forced", {"true": "blue", "false": "text"}),
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
                        status_colors("Status", {"NEW": "blue", "GONE": "orange"}, cell_type="color-text")
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
                        col_hidden("server_id"),
                        _QUERY_STORE_REGRESSION_HISTORY_LINK,
                        status_colors(
                            "Severity",
                            {
                                "CRITICAL": "red",
                                "HIGH": "orange",
                                "MEDIUM": "yellow",
                                "LOW": "green",
                            },
                        ),
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

    y = subtab(
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

    subtab(
        panels,
        "Long Queries",
        y,
        [
            (
                24,
                5,
                lambda x, y, w, h: table(
                    "Trace Status",
                    x,
                    y,
                    w,
                    h,
                    _TRACE_STATUS_SQL,
                    overrides=[
                        status_colors("Trace Status", {"ON": "green", "OFF": "red"}),
                    ],
                    description="Opt-in collector, defaults OFF. Enable it in the collector schedule config.",
                ),
            ),
            (
                24,
                16,
                lambda x, y, w, h: table(
                    "Long-Running Query Completions",
                    x,
                    y,
                    w,
                    h,
                    _LONG_QUERIES_SQL,
                    sort_by=[{"displayName": "Event Time", "desc": True}],
                    overrides=[
                        status_colors("Result", {"Abort": "red"}),
                        status_colors("Event Type", {"attention": "orange"}),
                        col_unit("Duration", "ms"),
                        col_unit("CPU", "ms"),
                    ],
                    description="Attentions (cancels/timeouts) carry no duration - the event has none.",
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


def procedure_history():
    """Per-collection history for one procedure, reached from the Top Procedures grid."""
    reset_id()
    panels: list[dict] = []

    y = subtab(
        panels,
        "History",
        0,
        [
            (
                24,
                8,
                lambda x, y, w, h: timeseries(
                    "${history_metric} over time (one dot per plan shape)",
                    x,
                    y,
                    w,
                    h,
                    [target(_PROCEDURE_HISTORY_CHART_SQL)],
                    points=True,
                    links=[
                        {
                            "title": "Show this plan shape below",
                            "url": "/d/darling-procedure-history?${__url_time_range}"
                            "&var-server=$server&var-database=$database"
                            "&var-schema=$schema&var-object_name=$object_name"
                            "&var-plan_shape=${__field.name}",
                            "targetBlank": False,
                        }
                    ],
                ),
            ),
        ],
    )
    y = subtab(
        panels,
        "History Grid",
        y,
        [
            (
                24,
                16,
                lambda x, y, w, h: table(
                    "Procedure history",
                    x,
                    y,
                    w,
                    h,
                    _PROCEDURE_HISTORY_SQL,
                    sort_by=[{"displayName": "Collection Time", "desc": True}],
                ),
            ),
        ],
    )

    subtab(
        panels,
        "Plan Shapes",
        y,
        [
            (
                24,
                8,
                lambda x, y, w, h: table(
                    "Plan shapes seen in range",
                    x,
                    y,
                    w,
                    h,
                    _PROCEDURE_PLAN_SHAPES_SQL,
                    sort_by=[{"displayName": "Last Seen", "desc": True}],
                    description="plan_handle identifies the plan shape - a recompile gets a "
                    "new handle. Pick a shape with the Plan Shape variable.",
                ),
            ),
            (
                24,
                4,
                lambda x, y, w, h: logs(
                    "Plan XML (${plan_shape})",
                    x,
                    y,
                    w,
                    h,
                    _PROCEDURE_PLAN_XML_SQL,
                    description="Compiled/cached plan (sys.dm_exec_text_query_plan) - not an "
                    "actual/runtime plan, since Grafana has no live SQL Server access here.",
                ),
            ),
            (
                24,
                8,
                lambda x, y, w, h: table(
                    "Compile-time parameters",
                    x,
                    y,
                    w,
                    h,
                    _PROCEDURE_PLAN_PARAMETERS_SQL,
                    description="Parsed from the plan XML's ParameterList. Runtime Value is "
                    "only ever populated on an actual (post-execution) plan, which this stored "
                    "plan is not - expect it blank here.",
                ),
            ),
        ],
    )

    return detail_dashboard(
        uid("procedure-history"),
        "Procedure History",
        panels,
        [
            server_var(),
            text_var("database", "Database", "*"),
            text_var("schema", "Schema", "*"),
            text_var("object_name", "Procedure", "*"),
            custom_var(
                "history_metric",
                "Chart Metric",
                _HISTORY_METRIC_OPTIONS,
                "Metric the trend chart plots.",
            ),
            single_query_var(
                "plan_shape",
                "Plan Shape",
                _PROCEDURE_PLAN_SHAPE_VAR_SQL,
                "Plan handle to show XML/parameters for, among those seen in the current time "
                "range.",
            ),
        ],
    )


def query_stats_history():
    """Per-collection history for one query hash, reached from the Top Queries grid."""
    reset_id()
    panels: list[dict] = []

    y = subtab(
        panels,
        "History",
        0,
        [
            (
                24,
                8,
                lambda x, y, w, h: timeseries(
                    "${history_metric} over time (one dot per plan shape)",
                    x,
                    y,
                    w,
                    h,
                    [target(_QUERY_STATS_HISTORY_CHART_SQL)],
                    points=True,
                    links=[
                        {
                            "title": "Show this plan shape below",
                            "url": "/d/darling-query-stats-history?${__url_time_range}"
                            "&var-server=$server&var-database=$database"
                            "&var-query_hash=$query_hash"
                            "&var-plan_shape=${__field.name}",
                            "targetBlank": False,
                        }
                    ],
                ),
            ),
        ],
    )
    y = subtab(
        panels,
        "History Grid",
        y,
        [
            (
                24,
                16,
                lambda x, y, w, h: table(
                    "Query history",
                    x,
                    y,
                    w,
                    h,
                    _QUERY_STATS_HISTORY_SQL,
                    sort_by=[{"displayName": "Collection Time", "desc": True}],
                ),
            ),
        ],
    )

    subtab(
        panels,
        "Plan Shapes",
        y,
        [
            (
                24,
                8,
                lambda x, y, w, h: table(
                    "Plan shapes seen in range",
                    x,
                    y,
                    w,
                    h,
                    _QUERY_PLAN_SHAPES_SQL,
                    sort_by=[{"displayName": "Last Seen", "desc": True}],
                    description="query_plan_hash identifies the plan shape - a recompile that "
                    "keeps the same shape but sniffs different parameters shows the same hash "
                    "here with different XML below. Pick a shape with the Plan Shape variable.",
                ),
            ),
            (
                24,
                4,
                lambda x, y, w, h: logs(
                    "Plan XML (${plan_shape})",
                    x,
                    y,
                    w,
                    h,
                    _QUERY_STATS_PLAN_XML_SQL,
                    description="Compiled/cached plan (sys.dm_exec_text_query_plan) - not an "
                    "actual/runtime plan, since Grafana has no live SQL Server access here.",
                ),
            ),
            (
                24,
                8,
                lambda x, y, w, h: table(
                    "Compile-time parameters",
                    x,
                    y,
                    w,
                    h,
                    _QUERY_STATS_PLAN_PARAMETERS_SQL,
                    description="Parsed from the plan XML's ParameterList. Runtime Value is "
                    "only ever populated on an actual (post-execution) plan, which this stored "
                    "plan is not - expect it blank here.",
                ),
            ),
        ],
    )

    return detail_dashboard(
        uid("query-stats-history"),
        "Query History",
        panels,
        [
            server_var(),
            text_var("database", "Database", "*"),
            text_var("query_hash", "Query Hash", "*"),
            custom_var(
                "history_metric",
                "Chart Metric",
                _HISTORY_METRIC_OPTIONS,
                "Metric the trend chart plots.",
            ),
            single_query_var(
                "plan_shape",
                "Plan Shape",
                _QUERY_PLAN_SHAPE_VAR_SQL,
                "Plan-cache shape (query_plan_hash) to show XML/parameters for, among those "
                "seen in the current time range.",
            ),
        ],
    )


def query_store_history():
    """Per-collection Query Store history for one query. Query-scoped, not plan-filtered."""
    reset_id()
    panels: list[dict] = []

    y = subtab(
        panels,
        "History",
        0,
        [
            (
                24,
                8,
                lambda x, y, w, h: timeseries(
                    "${qs_history_metric} over time (one dot per plan)",
                    x,
                    y,
                    w,
                    h,
                    [target(_QUERY_STORE_HISTORY_CHART_SQL)],
                    points=True,
                    links=[
                        {
                            "title": "Show this plan shape below",
                            "url": "/d/darling-query-store-history?${__url_time_range}"
                            "&var-server=$server&var-database=$database"
                            "&var-query_id=$query_id&var-plan_id=$plan_id"
                            "&var-plan_shape=${__field.name}",
                            "targetBlank": False,
                        }
                    ],
                ),
            ),
        ],
    )
    y = subtab(
        panels,
        "History Grid",
        y,
        [
            (
                24,
                16,
                lambda x, y, w, h: table(
                    "Query Store history",
                    x,
                    y,
                    w,
                    h,
                    _QUERY_STORE_HISTORY_SQL,
                    overrides=[
                        status_colors("Forced", {"true": "blue", "false": "text"})
                    ],
                    sort_by=[{"displayName": "Collection Time", "desc": True}],
                ),
            ),
        ],
    )

    subtab(
        panels,
        "Plan Shapes",
        y,
        [
            (
                24,
                8,
                lambda x, y, w, h: table(
                    "Plan shapes seen in range",
                    x,
                    y,
                    w,
                    h,
                    _QUERY_STORE_PLAN_SHAPES_SQL,
                    overrides=[
                        status_colors("Forced", {"true": "blue", "false": "text"})
                    ],
                    sort_by=[{"displayName": "Last Execution", "desc": True}],
                    description="plan_id is Query Store's own distinct-plan-shape id, "
                    "independent of the $plan_id filter above - this list always shows every "
                    "shape for the query in range. Pick one with the Plan Shape variable.",
                ),
            ),
            (
                24,
                4,
                lambda x, y, w, h: logs(
                    "Plan XML (${plan_shape})",
                    x,
                    y,
                    w,
                    h,
                    _QUERY_STORE_PLAN_XML_SQL,
                    description="Compiled plan from sys.query_store_plan - not an actual/"
                    "runtime plan, since Grafana has no live SQL Server access here.",
                ),
            ),
            (
                24,
                8,
                lambda x, y, w, h: table(
                    "Compile-time parameters",
                    x,
                    y,
                    w,
                    h,
                    _QUERY_STORE_PLAN_PARAMETERS_SQL,
                    description="Parsed from the plan XML's ParameterList. Runtime Value is "
                    "only ever populated on an actual (post-execution) plan, which this stored "
                    "plan is not - expect it blank here.",
                ),
            ),
        ],
    )

    return detail_dashboard(
        uid("query-store-history"),
        "Query Store History",
        panels,
        [
            server_var(),
            text_var("database", "Database", "*"),
            text_var("query_id", "Query ID", "*"),
            text_var("plan_id", "Plan ID", "*"),
            custom_var(
                "qs_history_metric",
                "Chart Metric",
                _QUERY_STORE_HISTORY_METRIC_OPTIONS,
                "Metric the trend chart plots.",
            ),
            single_query_var(
                "plan_shape",
                "Plan Shape",
                _QUERY_STORE_PLAN_SHAPE_VAR_SQL,
                "Query Store plan_id to show XML/parameters for, among those seen in the "
                "current time range (independent of the $plan_id filter above).",
            ),
        ],
    )
