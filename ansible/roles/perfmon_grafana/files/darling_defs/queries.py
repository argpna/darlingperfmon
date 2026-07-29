"""Queries dashboard (Darling line).

Top Resource Consumers only - proves rollup routing, not the full parity port.

Upstream ref: QueryPerformanceTab (Darling/PerformanceMonitor.Darling.Viewer/).
"""

from ._shared import (
    CAGG_TIME_COL,
    collector,
    dashboard,
    flow,
    reset_id,
    rollup,
    server_filter,
    server_var,
    subtab,
    table,
    target,
    tiered,
    timeseries,
    uid,
)


def queries():
    """Build the Queries dashboard."""
    reset_id()
    panels: list[dict] = []

    # Raw carries per-collection deltas; the CAGGs pre-aggregate them into *_sum over
    # bucket, so each branch spells out its own projection.
    top_consumers_sql = tiered(
        {
            "raw": f"""
SELECT
    qs.server_name AS "Server",
    qs.database_name AS "Database",
    qs.query_hash AS "Query Hash",
    SUM(qs.delta_worker_time) / 1000.0 AS "CPU (ms)",
    SUM(qs.delta_elapsed_time) / 1000.0 AS "Duration (ms)",
    SUM(qs.delta_execution_count) AS "Executions"
FROM {collector('query_stats')} AS qs
WHERE $__timeFilter(qs.collection_time)
  AND {server_filter('qs.server_id')}
GROUP BY qs.server_name, qs.database_name, qs.query_hash
""",
            "hourly": f"""
SELECT
    qs.server_name AS "Server",
    qs.database_name AS "Database",
    qs.query_hash AS "Query Hash",
    SUM(qs.worker_time_sum) / 1000.0 AS "CPU (ms)",
    SUM(qs.elapsed_time_sum) / 1000.0 AS "Duration (ms)",
    SUM(qs.execution_count_sum) AS "Executions"
FROM {rollup('query_stats', 'hourly')} AS qs
WHERE $__timeFilter(qs.{CAGG_TIME_COL})
  AND {server_filter('qs.server_id')}
GROUP BY qs.server_name, qs.database_name, qs.query_hash
""",
            "daily": f"""
SELECT
    qs.server_name AS "Server",
    qs.database_name AS "Database",
    qs.query_hash AS "Query Hash",
    SUM(qs.worker_time_sum) / 1000.0 AS "CPU (ms)",
    SUM(qs.elapsed_time_sum) / 1000.0 AS "Duration (ms)",
    SUM(qs.execution_count_sum) AS "Executions"
FROM {rollup('query_stats', 'daily')} AS qs
WHERE $__timeFilter(qs.{CAGG_TIME_COL})
  AND {server_filter('qs.server_id')}
GROUP BY qs.server_name, qs.database_name, qs.query_hash
""",
        }
    )

    cpu_trend_sql = tiered(
        {
            "raw": f"""
SELECT
    qs.collection_time AS time,
    qs.server_name AS metric,
    SUM(qs.delta_worker_time) / 1000.0 AS cpu_ms
FROM {collector('query_stats')} AS qs
WHERE $__timeFilter(qs.collection_time)
  AND {server_filter('qs.server_id')}
GROUP BY qs.collection_time, qs.server_name
""",
            "hourly": f"""
SELECT
    qs.{CAGG_TIME_COL} AS time,
    qs.server_name AS metric,
    SUM(qs.worker_time_sum) / 1000.0 AS cpu_ms
FROM {rollup('query_stats', 'hourly')} AS qs
WHERE $__timeFilter(qs.{CAGG_TIME_COL})
  AND {server_filter('qs.server_id')}
GROUP BY qs.{CAGG_TIME_COL}, qs.server_name
""",
            "daily": f"""
SELECT
    qs.{CAGG_TIME_COL} AS time,
    qs.server_name AS metric,
    SUM(qs.worker_time_sum) / 1000.0 AS cpu_ms
FROM {rollup('query_stats', 'daily')} AS qs
WHERE $__timeFilter(qs.{CAGG_TIME_COL})
  AND {server_filter('qs.server_id')}
GROUP BY qs.{CAGG_TIME_COL}, qs.server_name
""",
        }
    )

    y = subtab(
        panels,
        "Top Resource Consumers",
        0,
        [
            (
                24,
                9,
                lambda x, y, w, h: timeseries(
                    "Query CPU",
                    x,
                    y,
                    w,
                    h,
                    [target(f"{cpu_trend_sql}\nORDER BY 1")],
                    unit="ms",
                ),
            ),
        ],
    )

    flow(
        panels,
        y,
        [
            (
                24,
                12,
                lambda x, y, w, h: table(
                    "Top Queries by CPU",
                    x,
                    y,
                    w,
                    h,
                    f'{top_consumers_sql}\nORDER BY "CPU (ms)" DESC\nLIMIT 50',
                    sort_by=[{"displayName": "CPU (ms)", "desc": True}],
                ),
            )
        ],
    )

    return dashboard(uid("queries"), "Query Performance", panels, [server_var()])
