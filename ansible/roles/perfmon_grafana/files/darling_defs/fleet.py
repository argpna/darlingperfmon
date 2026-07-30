"""Fleet dashboard (Darling line).

Upstream ref: MainWindow.xaml's Overview tab, ViewerDataService.Overview.cs/.Fleet.cs,
ServerHealthBands.cs (ServerHealthClassifier).
"""

from ._shared import (
    collector,
    col_datalink,
    col_hidden,
    col_thresholds,
    dashboard,
    server_filter,
    server_var,
    stat,
    status_colors,
    table,
    thresholds,
    uid,
)
from .collection_health import _CADENCE_TABLE, _HEALTH_AGG, _HEALTH_STATUS

# ServerHealthThresholds: stale = 2x the 1-min collector cadence, offline = 15 min.
_STALE_MINUTES = 2
_OFFLINE_MINUTES = 15

# GetServerSummaryAsync's window: a fixed trailing hour, not the dashboard's time range.
_WINDOW = "(now() AT TIME ZONE 'UTC') - INTERVAL '1 hour'"

# Ends in the `fleet` CTE; agg/cadence/a reuse collection_health.py's 7-day collector banding.
_FLEET_CTE = f"""
WITH base AS (
    SELECT srv.server_id, srv.name AS server_name
    FROM config.config_monitored_servers AS srv
    WHERE srv.is_enabled AND {server_filter('srv.server_id')}
),
cpu AS (
    SELECT DISTINCT ON (server_id)
        server_id,
        sqlserver_cpu_utilization AS cpu_pct,
        other_process_cpu_utilization AS other_cpu_pct
    FROM {collector('cpu_utilization_stats')}
    ORDER BY server_id, sample_time DESC
),
threads AS (
    SELECT DISTINCT ON (server_id)
        server_id,
        max_workers_count AS total_threads,
        total_current_workers_count AS current_workers,
        total_runnable_tasks_count AS runnable_waiting,
        total_work_queue_count AS starved_requests
    FROM {collector('cpu_scheduler_stats')}
    ORDER BY server_id, collection_time DESC
),
memory_snap AS (
    SELECT DISTINCT ON (server_id)
        server_id, total_server_memory_mb, buffer_pool_mb
    FROM {collector('memory_stats')}
    ORDER BY server_id, collection_time DESC
),
memory_pressure AS (
    /* Every pool at the newest collection instant. */
    SELECT server_id,
        SUM(waiter_count) AS waiter_count,
        SUM(timeout_error_count_delta) AS timeout_count,
        SUM(forced_grant_count_delta) AS forced_count,
        SUM(granted_memory_mb) AS granted_mb
    FROM (
        SELECT server_id, waiter_count, timeout_error_count_delta,
               forced_grant_count_delta, granted_memory_mb,
               (collection_time = MAX(collection_time) OVER (PARTITION BY server_id))
                   AS is_latest
        FROM {collector('memory_grant_stats')}
    ) AS t
    WHERE is_latest
    GROUP BY server_id
),
blocking_xe AS (
    SELECT server_id, COUNT(*) AS cnt, MAX(wait_time_ms) AS max_wait_ms
    FROM {collector('blocked_process_reports')}
    WHERE event_time >= {_WINDOW} AND {server_filter()}
    GROUP BY server_id
),
blocking_dmv AS (
    SELECT server_id, COUNT(*) AS cnt, MAX(wait_time_ms) AS max_wait_ms
    FROM {collector('dmv_blocking_snapshots')}
    WHERE event_time >= {_WINDOW} AND {server_filter()}
    GROUP BY server_id
),
deadlocks AS (
    SELECT server_id, COUNT(*) AS cnt
    FROM {collector('deadlocks')}
    WHERE deadlock_time >= {_WINDOW} AND {server_filter()}
    GROUP BY server_id
),
agg AS ({_HEALTH_AGG}),
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
        COALESCE(EXTRACT(EPOCH FROM ((now() AT TIME ZONE 'UTC') - agg.last_success_time))
                 / 3600.0, 999) AS hours_since_success
    FROM agg
    LEFT JOIN cadence c ON c.collector_name = agg.collector_name
),
collectors AS (
    SELECT
        server_id,
        COUNT(*) FILTER (WHERE status = 'HEALTHY') AS healthy_count,
        COUNT(*) FILTER (WHERE status = 'FAILING') AS failing_count
    FROM (SELECT server_id, {_HEALTH_STATUS} AS status FROM a) AS x
    GROUP BY server_id
),
last_collection AS (
    SELECT server_id, MAX(collection_time) AS last_collection_time
    FROM {collector('collection_log')}
    GROUP BY server_id
),
metrics AS (
    SELECT
        b.server_id,
        b.server_name,
        cpu.cpu_pct,
        cpu.other_cpu_pct,
        COALESCE(cpu.cpu_pct, 0) + COALESCE(cpu.other_cpu_pct, 0) AS cpu_total_pct,
        th.total_threads,
        CASE WHEN th.total_threads IS NOT NULL
             THEN th.total_threads - COALESCE(th.current_workers, 0) END AS available_threads,
        COALESCE(th.runnable_waiting, 0) AS runnable_waiting,
        COALESCE(th.starved_requests, 0) AS starved_requests,
        ms.total_server_memory_mb,
        ms.buffer_pool_mb,
        mp.granted_mb AS memory_granted_mb,
        COALESCE(mp.waiter_count, 0) AS memory_waiter_count,
        COALESCE(mp.timeout_count, 0) AS memory_timeout_count,
        COALESCE(mp.forced_count, 0) AS memory_forced_count,
        /* XE preferred, DMV fallback - same source for the count and its worst wait. */
        COALESCE(NULLIF(bx.cnt, 0), bd.cnt, 0) AS blocking_count,
        COALESCE(CASE WHEN COALESCE(bx.cnt, 0) > 0 THEN bx.max_wait_ms ELSE bd.max_wait_ms END, 0)
            AS max_blocking_wait_ms,
        COALESCE(dl.cnt, 0) AS deadlock_count,
        COALESCE(co.healthy_count, 0) AS healthy_collector_count,
        COALESCE(co.failing_count, 0) AS failing_collector_count,
        lc.last_collection_time,
        EXTRACT(EPOCH FROM ((now() AT TIME ZONE 'UTC') - lc.last_collection_time)) / 60.0
            AS minutes_since_collection
    FROM base b
    LEFT JOIN cpu ON cpu.server_id = b.server_id
    LEFT JOIN threads th ON th.server_id = b.server_id
    LEFT JOIN memory_snap ms ON ms.server_id = b.server_id
    LEFT JOIN memory_pressure mp ON mp.server_id = b.server_id
    LEFT JOIN blocking_xe bx ON bx.server_id = b.server_id
    LEFT JOIN blocking_dmv bd ON bd.server_id = b.server_id
    LEFT JOIN deadlocks dl ON dl.server_id = b.server_id
    LEFT JOIN collectors co ON co.server_id = b.server_id
    LEFT JOIN last_collection lc ON lc.server_id = b.server_id
),
scored AS (
    SELECT
        m.*,
        /* ClassifyFreshness. */
        CASE
            WHEN m.last_collection_time IS NULL THEN 'NeverCollected'
            WHEN m.minutes_since_collection > {_OFFLINE_MINUTES} THEN 'Offline'
            WHEN m.minutes_since_collection > {_STALE_MINUTES} THEN 'Stale'
            ELSE 'Fresh'
        END AS freshness,
        /* Per-metric severity - ServerHealthClassifier.*Severity thresholds. */
        CASE WHEN m.cpu_pct IS NULL THEN 'Unknown'
             WHEN m.cpu_total_pct >= 95 THEN 'Critical'
             WHEN m.cpu_total_pct >= 80 THEN 'Warning'
             ELSE 'Healthy' END AS cpu_severity,
        CASE WHEN m.total_threads IS NULL THEN 'Unknown'
             WHEN m.starved_requests > 0 THEN 'Critical'
             WHEN m.runnable_waiting >= 20 THEN 'Warning'
             WHEN m.total_threads > 0 AND m.available_threads < m.total_threads * 0.10
                 THEN 'Warning'
             ELSE 'Healthy' END AS threads_severity,
        CASE WHEN m.memory_waiter_count > 0 OR m.memory_timeout_count > 0
                  OR m.memory_forced_count > 0
             THEN 'Critical' ELSE 'Healthy' END AS memory_severity,
        CASE WHEN m.max_blocking_wait_ms / 1000.0 >= 60 OR m.blocking_count >= 5 THEN 'Critical'
             WHEN m.max_blocking_wait_ms / 1000.0 >= 10 OR m.blocking_count >= 2 THEN 'Warning'
             WHEN m.blocking_count > 0 THEN 'Warning'
             ELSE 'Healthy' END AS blocking_severity,
        CASE WHEN m.deadlock_count > 0 THEN 'Critical' ELSE 'Healthy' END AS deadlock_severity,
        CASE WHEN m.failing_collector_count > 0 THEN 'Warning' ELSE 'Healthy' END
            AS collector_severity
    FROM metrics m
),
banded AS (
    SELECT
        s.*,
        /* OverallMetricSeverity - worst of the six; Unknown/Healthy never escalate. */
        CASE
            WHEN 'Critical' IN (s.cpu_severity, s.threads_severity, s.memory_severity,
                                 s.blocking_severity, s.deadlock_severity, s.collector_severity)
                THEN 'Critical'
            WHEN 'Warning' IN (s.cpu_severity, s.threads_severity, s.memory_severity,
                                s.blocking_severity, s.deadlock_severity, s.collector_severity)
                THEN 'Warning'
            ELSE 'Healthy'
        END AS overall_severity
    FROM scored s
),
fleet AS (
    SELECT
        b.*,
        /* FleetHealthBand.ClassifyBand. */
        CASE
            WHEN b.freshness = 'Offline' THEN 'Offline'
            WHEN b.freshness = 'NeverCollected' THEN 'Warning'
            WHEN b.overall_severity = 'Critical' THEN 'Critical'
            WHEN b.overall_severity = 'Warning' THEN 'Warning'
            WHEN b.freshness = 'Stale' THEN 'Warning'
            ELSE 'Healthy'
        END AS fleet_band,
        /* FleetHealthScore: band rank, then critical/warning counts, then incidents. */
        (CASE
            WHEN b.freshness = 'Offline' THEN 4000
            WHEN b.freshness = 'NeverCollected' THEN 2000
            WHEN b.overall_severity = 'Critical' THEN 3000
            WHEN b.overall_severity = 'Warning' THEN 2000
            WHEN b.freshness = 'Stale' THEN 2000
            ELSE 0
        END)
        + 100 * (
            (b.cpu_severity = 'Critical')::int + (b.threads_severity = 'Critical')::int
            + (b.memory_severity = 'Critical')::int + (b.blocking_severity = 'Critical')::int
            + (b.deadlock_severity = 'Critical')::int + (b.collector_severity = 'Critical')::int
        )
        + 10 * (
            (b.cpu_severity = 'Warning')::int + (b.threads_severity = 'Warning')::int
            + (b.memory_severity = 'Warning')::int + (b.blocking_severity = 'Warning')::int
            + (b.deadlock_severity = 'Warning')::int + (b.collector_severity = 'Warning')::int
        )
        + LEAST(b.blocking_count + b.deadlock_count, 99) AS fleet_score
    FROM banded b
)
"""


def _fleet_query(select_sql: str) -> str:
    return f"{_FLEET_CTE}\n{select_sql}"


# FleetRollup's header tiles - one query per panel, Grafana can't share a target.
_SUMMARY_TILES = (
    ("Servers", "COUNT(*)", thresholds(("blue", None))),
    (
        "Healthy",
        "COUNT(*) FILTER (WHERE fleet_band = 'Healthy')",
        thresholds(("green", None)),
    ),
    (
        "Warning",
        "COUNT(*) FILTER (WHERE fleet_band = 'Warning')",
        thresholds(("green", None), ("yellow", 1)),
    ),
    (
        "Critical",
        "COUNT(*) FILTER (WHERE fleet_band = 'Critical')",
        thresholds(("green", None), ("red", 1)),
    ),
    (
        "Offline",
        "COUNT(*) FILTER (WHERE fleet_band = 'Offline')",
        thresholds(("green", None), ("text", 1)),
    ),
    (
        "Blocking events (1h)",
        "COALESCE(SUM(blocking_count), 0)",
        thresholds(("green", None), ("yellow", 1)),
    ),
    (
        "Deadlocks (1h)",
        "COALESCE(SUM(deadlock_count), 0)",
        thresholds(("green", None), ("red", 1)),
    ),
    (
        "Collection failures",
        "COUNT(*) FILTER (WHERE failing_collector_count > 0)",
        thresholds(("green", None), ("red", 1)),
    ),
)

_TABLE_SQL = _fleet_query("""SELECT
    server_name AS "Server",
    server_id,
    CASE
        WHEN freshness = 'Offline' THEN 'Offline'
        WHEN freshness = 'NeverCollected' THEN 'Awaiting first collection'
        WHEN freshness = 'Stale' THEN 'Stale'
        ELSE 'Online'
    END AS "Status",
    ROUND(cpu_total_pct::numeric, 0) AS "CPU %",
    ROUND(cpu_pct::numeric, 0) AS "SQL CPU %",
    available_threads AS "Threads Available",
    total_threads AS "Threads Total",
    runnable_waiting AS "Runnable (waiting CPU)",
    starved_requests AS "Starved (no thread)",
    ROUND((total_server_memory_mb / 1024.0)::numeric, 1) AS "Total Memory GB",
    ROUND((buffer_pool_mb / 1024.0)::numeric, 1) AS "Buffer Pool GB",
    ROUND((memory_granted_mb / 1024.0)::numeric, 1) AS "Granted Memory GB",
    memory_waiter_count AS "Grant Waiters",
    memory_timeout_count AS "Grant Timeouts",
    memory_forced_count AS "Forced Grants",
    blocking_count AS "Blocking",
    ROUND((max_blocking_wait_ms / 1000.0)::numeric, 1) AS "Max Block (s)",
    deadlock_count AS "Deadlocks",
    healthy_collector_count AS "Collectors Healthy",
    failing_collector_count AS "Collectors Failing",
    last_collection_time AS "Last Collection",
    fleet_score AS "Severity"
FROM fleet
ORDER BY fleet_score DESC""")

_DRILLDOWN_URL = (
    f"/d/{uid('daily-summary')}?${{__url_time_range}}"
    "&var-server=${__data.fields.server_id}"
)


def fleet():
    """Build the Fleet dashboard: fleet-wide summary tiles + one sortable per-server table."""
    panels: list[dict] = []

    x = 0
    tile_w = 24 // len(_SUMMARY_TILES)
    for title, expr, th in _SUMMARY_TILES:
        sql = _fleet_query(f"SELECT {expr} AS value FROM fleet")
        panels.append(stat(title, x, 0, tile_w, 4, sql, "short", th))
        x += tile_w

    overrides = [
        col_hidden("server_id"),
        col_datalink("Server", "Open Daily Summary", _DRILLDOWN_URL),
        status_colors(
            "Status",
            {
                "Online": "green",
                "Stale": "yellow",
                "Offline": "red",
                "Awaiting first collection": "text",
            },
        ),
        col_thresholds("CPU %", ("green", None), ("yellow", 80), ("red", 95)),
        col_thresholds("Runnable (waiting CPU)", ("green", None), ("yellow", 20)),
        col_thresholds("Starved (no thread)", ("green", None), ("red", 1)),
        col_thresholds("Grant Waiters", ("green", None), ("red", 1)),
        col_thresholds("Grant Timeouts", ("green", None), ("red", 1)),
        col_thresholds("Forced Grants", ("green", None), ("red", 1)),
        col_thresholds("Blocking", ("green", None), ("yellow", 2), ("red", 5)),
        col_thresholds("Max Block (s)", ("green", None), ("yellow", 10), ("red", 60)),
        col_thresholds("Deadlocks", ("transparent", None), ("red", 1)),
        col_thresholds("Collectors Failing", ("transparent", None), ("red", 1)),
        col_thresholds(
            "Severity", ("transparent", None), ("yellow", 2000), ("red", 3000)
        ),
    ]

    panels.append(
        table(
            "Fleet health",
            0,
            4,
            24,
            18,
            _TABLE_SQL,
            overrides=overrides,
            sort_by=[{"displayName": "Severity", "desc": True}],
            description=(
                "One row per monitored server - click any column header to sort. "
                "Click a server's name to open its Daily Summary."
            ),
        )
    )

    return dashboard(
        uid("fleet"),
        "Fleet Overview",
        panels,
        [server_var(multi=True)],
        time_from="now-1h",
        refresh="1m",
    )
