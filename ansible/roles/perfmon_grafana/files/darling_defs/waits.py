"""Wait Stats section (Darling line) - the Wait Stats half of the Wait Analysis dashboard
(wait_analysis.py), plus the Wait Drill-Down nav-only dashboard it links to.

Upstream ref: ViewerServerTab.Waits.cs / ViewerDataService.Waits.cs.

The upstream tab is a wait-type picker driving one chart with a two-option metric combo.
The picker becomes $wait_type; the combo's options become their own panels, since a Grafana
panel has no combo and showing both costs nothing.

Upstream ref: WaitDrillDownWindow.xaml.cs. wait_drill_down() below only implements the
Filtered path of WaitDrillDownHelper.Classify, matching dashboard_defs/wait_drill_down.py's
precedent - Correlated, Uncapturable, and Chain are not ported.
"""

from ._shared import (
    col_unit,
    collector,
    detail_dashboard,
    multi_filter,
    query_var,
    reset_id,
    server_filter,
    server_join,
    server_var,
    stat_grid,
    subtab,
    table,
    target,
    thresholds,
    timeseries,
    uid,
)

# wait_stats has no rollup and no retention policy, so every panel reads raw.
_WAIT_TYPES_QUERY = f"""
SELECT ws.wait_type
FROM {collector('wait_stats')} AS ws
WHERE $__timeFilter(ws.collection_time)
  AND {server_filter('ws.server_id')}
GROUP BY ws.wait_type
ORDER BY SUM(ws.delta_wait_time_ms) DESC
"""


def _trend_sql(metric_expr: str) -> str:
    """Per-collection trend for the selected wait types, capped at upstream's 20 series.

    interval_seconds is the truncate-then-diff epoch idiom upstream uses. $server is
    single-select, so the legend is just the wait type - no server label to disambiguate.
    """
    return f"""
WITH ranked AS (
    SELECT ws.server_id, ws.wait_type
    FROM {collector('wait_stats')} AS ws
    WHERE $__timeFilter(ws.collection_time)
      AND {server_filter('ws.server_id')}
      AND {multi_filter('ws.wait_type', 'wait_type')}
    GROUP BY ws.server_id, ws.wait_type
    ORDER BY SUM(ws.delta_wait_time_ms) DESC
    LIMIT 20
),
windowed AS (
    SELECT
        ws.wait_type,
        ws.collection_time,
        ws.delta_wait_time_ms,
        ws.delta_signal_wait_time_ms,
        ws.delta_waiting_tasks,
        EXTRACT(EPOCH FROM (date_trunc('second', ws.collection_time)
            - date_trunc('second', LAG(ws.collection_time) OVER (
                PARTITION BY ws.server_id, ws.wait_type
                ORDER BY ws.collection_time)))) AS interval_seconds
    FROM {collector('wait_stats')} AS ws
    JOIN ranked AS r
      ON r.server_id = ws.server_id
     AND r.wait_type = ws.wait_type
    WHERE $__timeFilter(ws.collection_time)
      AND {server_filter('ws.server_id')}
)
SELECT
    collection_time AS time,
    wait_type AS metric,
    {metric_expr} AS value
FROM windowed
ORDER BY 1
"""


_WAIT_MS_SEC = """CASE
        WHEN interval_seconds > 0
        THEN delta_wait_time_ms::double precision / interval_seconds
        ELSE 0
    END"""

_SIGNAL_MS_SEC = """CASE
        WHEN interval_seconds > 0
        THEN delta_signal_wait_time_ms::double precision / interval_seconds
        ELSE 0
    END"""

_AVG_MS_PER_WAIT = """CASE
        WHEN delta_waiting_tasks > 0
        THEN delta_wait_time_ms::double precision / delta_waiting_tasks
        ELSE 0
    END"""

# Upstream ref: GetQuerySnapshotsByWaitTypeAsync (ViewerDataService.QuerySnapshots.cs)
# No $database filter here - waits() has no $database var; wait_drill_down() adds its own.
_SNAPSHOT_COUNT_SQL = f"""
SELECT COUNT(*) AS "Query Snapshots"
FROM {collector('query_snapshots')} AS qs
WHERE $__timeFilter(qs.collection_time)
  AND {server_filter('qs.server_id')}
  AND {multi_filter('qs.wait_type', 'wait_type')}
  AND qs.query_text NOT LIKE 'WAITFOR%'
"""

_WAIT_DRILL_DOWN_LINK = {
    "title": "View Query Snapshots",
    "url": "/d/darling-wait-drill-down?${__url_time_range}"
    "&var-server=$server&var-wait_type=$wait_type",
    "targetBlank": False,
}

_DATABASE_VAR_SQL = f"""
SELECT DISTINCT qs.database_name
FROM {collector('query_snapshots')} AS qs
WHERE $__timeFilter(qs.collection_time) AND {server_filter('qs.server_id')}
  AND qs.database_name IS NOT NULL
ORDER BY 1
"""

_WAIT_DRILL_DOWN_SQL = f"""
SELECT
    srv.name AS "Server",
    qs.collection_time AS "Collected",
    qs.session_id AS "SPID",
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
    qs.requested_memory_mb AS "Req Mem (MB)",
    qs.used_memory_mb AS "Used Mem (MB)",
    qs.max_used_memory_mb AS "Max Used Mem (MB)",
    qs.tempdb_current_mb AS "tempdb Cur (MB)",
    qs.tempdb_allocations_mb AS "tempdb Alloc (MB)",
    qs.tran_log_used_mb AS "Tran Log (MB)",
    qs.tran_start_time AS "Tran Start",
    qs.request_id AS "Req ID",
    qs.transaction_isolation_level AS "Isolation",
    qs.open_transaction_count AS "Open Tran",
    qs.percent_complete AS "% Done",
    qs.query_hash AS "Query Hash",
    qs.query_text AS "Query Text"
FROM {collector('query_snapshots')} AS qs
{server_join('qs.server_id')}
WHERE $__timeFilter(qs.collection_time)
  AND {server_filter('qs.server_id')}
  AND {multi_filter('qs.wait_type', 'wait_type')}
  AND {multi_filter('qs.database_name', 'database')}
  AND qs.query_text NOT LIKE 'WAITFOR%'
ORDER BY qs.collection_time DESC, qs.cpu_time_ms DESC
LIMIT 500
"""


def wait_type_var():
    """Build the $wait_type variable, shared by the Wait Stats section and its drill-down."""
    return query_var(
        "wait_type",
        "Wait type",
        _WAIT_TYPES_QUERY,
        "Wait types collected over the window, heaviest first. All plots the top 20.",
    )


def wait_stats_section(panels: list[dict], y: int) -> int:
    """Append the Wait Stats section (row header + grid) to the Wait Analysis dashboard.
    Returns the y below it, for the caller's next section."""
    return subtab(
        panels,
        "Wait Stats",
        y,
        [
            (
                24,
                10,
                lambda x, y, w, h: timeseries(
                    "Wait Time (ms/sec)",
                    x,
                    y,
                    w,
                    h,
                    [target(_trend_sql(_WAIT_MS_SEC))],
                    unit="ms",
                    axis_label="ms/sec",
                ),
            ),
            (
                12,
                9,
                lambda x, y, w, h: timeseries(
                    "Avg Wait Time (ms/wait)",
                    x,
                    y,
                    w,
                    h,
                    [target(_trend_sql(_AVG_MS_PER_WAIT))],
                    unit="ms",
                    axis_label="ms/wait",
                ),
            ),
            # Upstream computes signal wait per second in the same read but its metric
            # combo does not expose it; charting it costs nothing and shows CPU pressure.
            (
                12,
                9,
                lambda x, y, w, h: timeseries(
                    "Signal Wait Time (ms/sec)",
                    x,
                    y,
                    w,
                    h,
                    [target(_trend_sql(_SIGNAL_MS_SEC))],
                    unit="ms",
                    axis_label="ms/sec",
                ),
            ),
            # Upstream ref: "Show Queries With This Wait" (ViewerServerTab.DrillDown.cs). A
            # full-width stat_grid() banner rather than a small hand-placed tile stranded
            # alone on its own row - the fix for this dashboard's named orphaned-stat issue.
            (
                24,
                4,
                stat_grid(
                    [
                        {
                            "title": "Query Snapshots ($wait_type)",
                            "sql": _SNAPSHOT_COUNT_SQL,
                            "th": thresholds(("blue", None)),
                        }
                    ],
                    cols=1,
                ),
            ),
        ],
    )


def wait_drill_down():
    """Query snapshots matching the selected wait type(s).

    Reached from the Wait Stats dashboard's "Query Snapshots" linking stat tile.
    """
    reset_id()
    panels: list[dict] = []

    subtab(
        panels,
        "Query Snapshots",
        0,
        [
            (
                24,
                16,
                lambda x, y, w, h: table(
                    "Query snapshots with the selected wait type",
                    x,
                    y,
                    w,
                    h,
                    _WAIT_DRILL_DOWN_SQL,
                    overrides=[
                        col_unit("CPU (ms)", "ms"),
                        col_unit("Wait (ms)", "ms"),
                    ],
                    sort_by=[{"displayName": "Collected", "desc": True}],
                    description=(
                        "sp_WhoIsActive snapshots whose wait_type matches $wait_type, top "
                        "500 by collection time then CPU. Only the Filtered classification "
                        "is ported."
                    ),
                ),
            ),
        ],
    )

    database_var = query_var(
        "database",
        "Database",
        _DATABASE_VAR_SQL,
        "Databases seen in query snapshots over the window.",
    )

    return detail_dashboard(
        uid("wait-drill-down"),
        "Wait Drill-Down",
        panels,
        [
            server_var(),
            query_var(
                "wait_type",
                "Wait type",
                _WAIT_TYPES_QUERY,
                "Wait types collected over the window, heaviest first.",
            ),
            database_var,
        ],
        time_from="now-3h",
    )
