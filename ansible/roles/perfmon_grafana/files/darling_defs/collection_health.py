"""Collection Health dashboard (Darling line).

Upstream ref: ViewerDataService.CollectionHealth.cs, ViewerServerTab.CollectionHealth.cs,
CollectorHealthClassifier (PerformanceMonitor.Common), CollectorScheduleDefaults.

The tab's "Purge Now" button is not ported: it enqueues a control command for the service to
run, and Grafana only reads the store.

Upstream ref: CollectionLogWindow.xaml.cs. collection_log_detail() below reads a hard-coded
trailing 168 hours, not $__timeFilter like the rest of this line - matches upstream's own quirk.
"""

from ._shared import (
    col_datalink,
    col_hidden,
    collector,
    col_thresholds,
    col_unit,
    dashboard,
    detail_dashboard,
    reset_id,
    server_filter,
    server_join,
    server_var,
    status_colors,
    stat,
    subtab,
    table,
    target,
    text_var,
    thresholds,
    timeseries,
    uid,
)

_HEALTH_COLORS = {
    "HEALTHY": "green",
    "WARNING": "yellow",
    "STALE": "orange",
    "FAILING": "red",
    "NO_PERMISSIONS": "purple",
    "NEVER_RUN": "text",
}

# CollectorHealthClassifier thresholds.
_WARNING_FAILURE_RATE = 20.0
_FAILING_FLOOR_HOURS = 24.0
_FAILING_CADENCE_MULTIPLIER = 2.0
_STALE_FLOOR_HOURS = 4.0
_STALE_CADENCE_MULTIPLIER = 1.5

# Collectors that run once on load, so staleness never applies to them.
_ON_LOAD = (
    "server_config",
    "database_config",
    "database_scoped_config",
    "trace_flags",
    "server_properties",
)

# CollectorScheduleDefaults.All frequencies, in minutes. Banding needs the collector's own
# cadence or a slow collector reads as stale between its own runs. A name missing here (the
# service's own run-records, a collector added upstream) gets 0 and falls to the floors,
# which is what upstream's failed TryGetValue does.
_FREQUENCY_MINUTES = {
    "ag_database_replica_states": 1,
    "ag_replica_states": 1,
    "agent_status": 5,
    "blocked_process_report": 1,
    "cpu_scheduler_stats": 1,
    "cpu_utilization": 1,
    "database_config": 0,
    "database_scoped_config": 0,
    "database_size_stats": 60,
    "deadlocks": 1,
    "default_trace_events": 5,
    "dmv_blocking_snapshot": 1,
    "file_io_stats": 1,
    "index_object_stats": 1440,
    "job_history": 5,
    "latch_stats": 1,
    "long_query_completions": 1,
    "memory_clerks": 5,
    "memory_grant_stats": 1,
    "memory_pressure_events": 5,
    "memory_stats": 1,
    "perfmon_stats": 1,
    "plan_cache_stats": 5,
    "procedure_stats": 1,
    "query_snapshots": 1,
    "query_stats": 1,
    "query_store": 5,
    "running_jobs": 5,
    "server_config": 0,
    "server_properties": 0,
    "session_stats": 5,
    "session_summary_stats": 5,
    "spinlock_stats": 1,
    "system_health_events": 5,
    "tempdb_stats": 1,
    "trace_flags": 0,
    "wait_stats": 1,
    "waiting_tasks": 1,
}

_CADENCE_TABLE = ",\n        ".join(
    f"('{name}', {minutes})" for name, minutes in sorted(_FREQUENCY_MINUTES.items())
)

_ON_LOAD_LIST = ", ".join(f"'{name}'" for name in _ON_LOAD)

# Upstream ref: CollectorHealthClassifier.Classify - first-match-wins, on-load collectors
# skip the staleness rungs entirely.
_HEALTH_STATUS = f"""CASE
        WHEN a.total_runs = 0 THEN 'NEVER_RUN'
        WHEN a.permission_denied_count > 0 AND a.error_count = 0 AND a.success_count = 0
            THEN 'NO_PERMISSIONS'
        WHEN a.collector_name IN ({_ON_LOAD_LIST})
            THEN CASE WHEN a.failure_rate > {_WARNING_FAILURE_RATE} THEN 'WARNING'
                      ELSE 'HEALTHY' END
        WHEN a.hours_since_success
             > GREATEST({_FAILING_FLOOR_HOURS},
                        {_FAILING_CADENCE_MULTIPLIER} * a.frequency_minutes / 60.0)
            THEN 'FAILING'
        WHEN a.hours_since_success
             > GREATEST({_STALE_FLOOR_HOURS},
                        {_STALE_CADENCE_MULTIPLIER} * a.frequency_minutes / 60.0)
            THEN 'STALE'
        WHEN a.failure_rate > {_WARNING_FAILURE_RATE} THEN 'WARNING'
        ELSE 'HEALTHY'
    END"""

# The Health Summary window is a fixed trailing 7 days, NOT the dashboard's time range:
# staleness banding is meaningless against a window the reader can shrink to an hour.
# Upstream pins it the same way and for the same reason.
_HEALTH_WINDOW = "(now() AT TIME ZONE 'UTC') - INTERVAL '7 days'"

_HEALTH_AGG = f"""
    SELECT
        cl.server_id,
        cl.collector_name,
        COUNT(*) AS total_runs,
        SUM(CASE WHEN cl.status = 'SUCCESS' THEN 1 ELSE 0 END) AS success_count,
        SUM(CASE WHEN cl.status = 'ERROR' THEN 1 ELSE 0 END) AS error_count,
        AVG(cl.duration_ms) AS avg_duration_ms,
        /* SKIPPED counts as a healthy run: dedup and version-gated collectors no-op
           without being stale. */
        MAX(CASE WHEN cl.status IN ('SUCCESS', 'SKIPPED') THEN cl.collection_time END)
            AS last_success_time,
        MAX(cl.collection_time) AS last_run_time,
        MAX(CASE WHEN cl.status IN ('ERROR', 'PERMISSIONS') THEN cl.error_message END)
            AS last_error,
        MAX(CASE WHEN cl.status IN ('ERROR', 'PERMISSIONS') THEN cl.collection_time END)
            AS last_error_time,
        SUM(CASE WHEN cl.status = 'PERMISSIONS' THEN 1 ELSE 0 END)
            AS permission_denied_count,
        /* YIELDED = the 1s lock-timeout guard fired: deliberate and benign for collection,
           counted apart from errors because it says something about the monitored server's
           lock contention rather than about monitoring. */
        SUM(CASE WHEN cl.status = 'YIELDED' THEN 1 ELSE 0 END) AS yield_count
    FROM {collector('collection_log')} AS cl
    WHERE cl.collection_time >= {_HEALTH_WINDOW}
      AND {server_filter('cl.server_id')}
    GROUP BY cl.server_id, cl.collector_name
"""

_HEALTH_SQL = f"""
WITH agg AS ({_HEALTH_AGG}),
cadence(collector_name, frequency_minutes) AS (VALUES
        {_CADENCE_TABLE}
),
a AS (
    SELECT
        agg.*,
        COALESCE(c.frequency_minutes, 0) AS frequency_minutes,
        CASE WHEN agg.total_runs > 0
             THEN agg.error_count::double precision / agg.total_runs * 100
             ELSE 0 END AS failure_rate,
        /* Upstream's "never succeeded" sentinel, so a collector with no success ever lands
           past every threshold instead of comparing against NULL. */
        COALESCE(EXTRACT(EPOCH FROM ((now() AT TIME ZONE 'UTC') - agg.last_success_time))
                 / 3600.0, 999) AS hours_since_success
    FROM agg
    LEFT JOIN cadence c ON c.collector_name = agg.collector_name
)
SELECT
    a.server_id AS "server_id",
    srv.name AS "Server",
    a.collector_name AS "Collector",
    {_HEALTH_STATUS} AS "Status",
    a.total_runs AS "Runs",
    a.success_count AS "Success",
    a.error_count AS "Errors",
    a.yield_count AS "Yields",
    a.permission_denied_count AS "Denied",
    a.failure_rate AS "Fail %",
    a.avg_duration_ms AS "Avg Duration",
    a.last_success_time AS "Last Success",
    a.last_run_time AS "Last Run",
    a.last_error_time AS "Last Error At",
    a.last_error AS "Last Error"
FROM a
{server_join('a.server_id')}
ORDER BY srv.name, a.collector_name
"""

# Upstream ref: PermissionDeniedCollectorCountSql (#1591) - the tab header's badge. Counts
# collectors, not rows, so one collector failing every cycle for a week reads as 1.
_DENIED_SQL = f"""
SELECT COUNT(DISTINCT cl.collector_name) AS "Permission-Denied Collectors"
FROM {collector('collection_log')} AS cl
WHERE cl.collection_time >= {_HEALTH_WINDOW}
  AND {server_filter('cl.server_id')}
  AND cl.status = 'PERMISSIONS'
"""

# duckdb_duration_ms is the legacy column name; in the Darling store it records the Postgres
# write phase, which is why upstream labels it "Store (ms)".
_LOG_SQL = f"""
SELECT
    cl.collection_time AS "Time",
    srv.name AS "Server",
    cl.collector_name AS "Collector",
    cl.status AS "Status",
    cl.duration_ms AS "Duration",
    cl.sql_duration_ms AS "SQL (ms)",
    cl.duckdb_duration_ms AS "Store (ms)",
    cl.rows_collected AS "Rows",
    cl.error_message AS "Error"
FROM {collector('collection_log')} AS cl
{server_join('cl.server_id')}
WHERE $__timeFilter(cl.collection_time)
  AND {server_filter('cl.server_id')}
ORDER BY cl.collection_time DESC
LIMIT 500
"""

# Successful runs only, as upstream's scatter plots: a failed run's duration measures how
# long it took to fail, which is not a collection-cost trend.
_DURATION_SQL = f"""
SELECT
    cl.collection_time AS time,
    srv.name || ' - ' || cl.collector_name AS metric,
    cl.duration_ms AS value
FROM {collector('collection_log')} AS cl
{server_join('cl.server_id')}
WHERE $__timeFilter(cl.collection_time)
  AND {server_filter('cl.server_id')}
  AND cl.status = 'SUCCESS'
  AND cl.duration_ms IS NOT NULL
ORDER BY 1
"""


_COLLECTOR_LOG_LINK = col_datalink(
    "Collector",
    "View collection log for this collector",
    "/d/darling-collection-log-detail?${__url_time_range}"
    "&var-server=${__data.fields.server_id}"
    "&var-collector_name=${__data.fields.Collector}",
)

# Upstream ref: GetCollectionLogByCollectorAsync (ViewerDataService.CollectionHealth.cs)
_COLLECTOR_LOG_WINDOW = "(now() AT TIME ZONE 'UTC') - INTERVAL '168 hours'"

_COLLECTOR_LOG_WHERE = f"""
{server_filter('cl.server_id')}
  AND (${{collector_name:sqlstring}} = '*' OR ${{collector_name:sqlstring}} = ''
       OR cl.collector_name = ${{collector_name:sqlstring}})
  AND cl.collection_time >= {_COLLECTOR_LOG_WINDOW}
"""

# Upstream ref: CollectionLogWindow_Loaded
_COLLECTOR_LOG_SUMMARY_SQL = f"""
SELECT
    COUNT(*) AS "Total Runs",
    SUM(CASE WHEN cl.status = 'SUCCESS' THEN 1 ELSE 0 END) AS "Success",
    SUM(CASE WHEN cl.status = 'ERROR' THEN 1 ELSE 0 END) AS "Errors",
    AVG(cl.duration_ms) FILTER (WHERE cl.status = 'SUCCESS') AS "Avg Duration"
FROM {collector('collection_log')} AS cl
WHERE {_COLLECTOR_LOG_WHERE}
"""

_COLLECTOR_LOG_SQL = f"""
SELECT
    cl.collection_time AS "Time",
    cl.status AS "Status",
    cl.rows_collected AS "Rows",
    cl.duration_ms AS "Duration",
    cl.sql_duration_ms AS "SQL Duration",
    cl.duckdb_duration_ms AS "Store Duration",
    cl.error_message AS "Error"
FROM {collector('collection_log')} AS cl
WHERE {_COLLECTOR_LOG_WHERE}
ORDER BY cl.collection_time DESC
"""


def collection_health():
    """Build the Collection Health dashboard."""
    reset_id()
    panels: list[dict] = []

    y = subtab(
        panels,
        "Health Summary",
        0,
        [
            (
                6,
                4,
                lambda x, y, w, h: stat(
                    "Permission-Denied Collectors",
                    x,
                    y,
                    w,
                    h,
                    _DENIED_SQL,
                    "short",
                    thresholds(("green", None), ("red", 1)),
                ),
            ),
            (
                24,
                13,
                lambda x, y, w, h: table(
                    "Collector Health",
                    x,
                    y,
                    w,
                    h,
                    _HEALTH_SQL,
                    overrides=[
                        col_hidden("server_id"),
                        _COLLECTOR_LOG_LINK,
                        status_colors("Status", _HEALTH_COLORS),
                        col_unit("Avg Duration", "ms"),
                        col_unit("Fail %", "percent"),
                        col_thresholds(
                            "Fail %", ("green", None), ("yellow", _WARNING_FAILURE_RATE)
                        ),
                        col_thresholds("Errors", ("text", None), ("red", 1)),
                    ],
                    sort_by=[{"displayName": "Status", "desc": False}],
                    description=(
                        "Trailing 7 days, independent of the dashboard time range - "
                        "staleness banding needs a stable horizon."
                    ),
                ),
            ),
        ],
    )

    y = subtab(
        panels,
        "Collection Log",
        y,
        [
            (
                24,
                13,
                lambda x, y, w, h: table(
                    "Recent Collection Runs",
                    x,
                    y,
                    w,
                    h,
                    _LOG_SQL,
                    overrides=[
                        status_colors(
                            "Status",
                            {
                                "SUCCESS": "green",
                                "SKIPPED": "text",
                                "YIELDED": "yellow",
                                "PERMISSIONS": "purple",
                                "ERROR": "red",
                            },
                        ),
                        col_unit("Duration", "ms"),
                        col_unit("SQL (ms)", "ms"),
                        col_unit("Store (ms)", "ms"),
                    ],
                    sort_by=[{"displayName": "Time", "desc": True}],
                ),
            )
        ],
    )

    subtab(
        panels,
        "Duration Trends",
        y,
        [
            (
                24,
                11,
                lambda x, y, w, h: timeseries(
                    "Collector Duration",
                    x,
                    y,
                    w,
                    h,
                    [target(_DURATION_SQL)],
                    unit="ms",
                    axis_label="Duration (ms)",
                ),
            )
        ],
    )

    return dashboard(
        uid("collection-health"), "Collection Health", panels, [server_var()]
    )


def collection_log_detail():
    """Collection log for one collector, reached from the Health Summary grid's Collector
    column. Fixed trailing 168h window - see the module docstring."""
    reset_id()
    panels: list[dict] = []

    stat_grid_row_h = 4
    subtab(
        panels,
        "Collector Log",
        0,
        [
            (
                6,
                stat_grid_row_h,
                lambda x, y, w, h: stat(
                    "Total Runs",
                    x,
                    y,
                    w,
                    h,
                    _COLLECTOR_LOG_SUMMARY_SQL,
                    "short",
                    thresholds(("text", None)),
                    fields="/^Total Runs$/",
                ),
            ),
            (
                6,
                stat_grid_row_h,
                lambda x, y, w, h: stat(
                    "Success",
                    x,
                    y,
                    w,
                    h,
                    _COLLECTOR_LOG_SUMMARY_SQL,
                    "short",
                    thresholds(("green", None)),
                    fields="/^Success$/",
                ),
            ),
            (
                6,
                stat_grid_row_h,
                lambda x, y, w, h: stat(
                    "Errors",
                    x,
                    y,
                    w,
                    h,
                    _COLLECTOR_LOG_SUMMARY_SQL,
                    "short",
                    thresholds(("green", None), ("red", 1)),
                    fields="/^Errors$/",
                ),
            ),
            (
                6,
                stat_grid_row_h,
                lambda x, y, w, h: stat(
                    "Avg Duration",
                    x,
                    y,
                    w,
                    h,
                    _COLLECTOR_LOG_SUMMARY_SQL,
                    "ms",
                    thresholds(("text", None)),
                    fields="/^Avg Duration$/",
                ),
            ),
            (
                24,
                14,
                lambda x, y, w, h: table(
                    "Collection runs - $collector_name (trailing 168h)",
                    x,
                    y,
                    w,
                    h,
                    _COLLECTOR_LOG_SQL,
                    overrides=[
                        status_colors(
                            "Status",
                            {
                                "SUCCESS": "green",
                                "SKIPPED": "text",
                                "YIELDED": "yellow",
                                "PERMISSIONS": "purple",
                                "ERROR": "red",
                            },
                        ),
                        col_unit("Duration", "ms"),
                        col_unit("SQL Duration", "ms"),
                        col_unit("Store Duration", "ms"),
                    ],
                    sort_by=[{"displayName": "Time", "desc": True}],
                ),
            ),
        ],
    )

    return detail_dashboard(
        uid("collection-log-detail"),
        "Collection Log Detail",
        panels,
        [server_var(), text_var("collector_name", "Collector", "*")],
        time_from="now-7d",
    )
