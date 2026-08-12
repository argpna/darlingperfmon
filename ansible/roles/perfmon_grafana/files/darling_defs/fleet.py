"""Fleet dashboard.

Upstream ref: MainWindow.xaml's Overview tab, ViewerDataService.Overview.cs/.Fleet.cs,
ServerHealthBands.cs (ServerHealthClassifier).
"""

from ._shared import (
    HEALTH_STATUS_COLORS,
    alertlist,
    collector,
    col_datalink,
    col_datalinks,
    col_hidden,
    col_pills,
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

# Alert rules for this line are provisioned into this folder (alerting_darling.yml); must
# match grafana_darling_folder_uid/grafana_darling_folder in defaults/main.yml.
_ALERT_FOLDER_UID = "perfmon-darling"
_ALERT_FOLDER_TITLE = "PerformanceMonitor (Darling)"

_NOT_FRESH = (
    f"(m.last_collection_time IS NULL OR m.minutes_since_collection > {_STALE_MINUTES})"
)

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
        /* Per-metric severity - ServerHealthClassifier.*Severity thresholds. Every branch
           gates on {_NOT_FRESH} first (see its comment above). */
        CASE WHEN {_NOT_FRESH} OR m.cpu_pct IS NULL THEN 'Unknown'
             WHEN m.cpu_total_pct >= 95 THEN 'Critical'
             WHEN m.cpu_total_pct >= 80 THEN 'Warning'
             ELSE 'Healthy' END AS cpu_severity,
        CASE WHEN {_NOT_FRESH} OR m.total_threads IS NULL THEN 'Unknown'
             WHEN m.starved_requests > 0 THEN 'Critical'
             WHEN m.runnable_waiting >= 20 THEN 'Warning'
             WHEN m.total_threads > 0 AND m.available_threads < m.total_threads * 0.10
                 THEN 'Warning'
             ELSE 'Healthy' END AS threads_severity,
        CASE WHEN {_NOT_FRESH} THEN 'Unknown'
             WHEN m.memory_waiter_count > 0 OR m.memory_timeout_count > 0
                  OR m.memory_forced_count > 0
             THEN 'Critical' ELSE 'Healthy' END AS memory_severity,
        CASE WHEN {_NOT_FRESH} THEN 'Unknown'
             WHEN m.max_blocking_wait_ms / 1000.0 >= 60 OR m.blocking_count >= 5 THEN 'Critical'
             WHEN m.max_blocking_wait_ms / 1000.0 >= 10 OR m.blocking_count >= 2 THEN 'Warning'
             WHEN m.blocking_count > 0 THEN 'Warning'
             ELSE 'Healthy' END AS blocking_severity,
        CASE WHEN {_NOT_FRESH} THEN 'Unknown'
             WHEN m.deadlock_count > 0 THEN 'Critical' ELSE 'Healthy' END AS deadlock_severity,
        CASE WHEN {_NOT_FRESH} THEN 'Unknown'
             WHEN m.failing_collector_count > 0 THEN 'Warning' ELSE 'Healthy' END
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


_BAND_TILES = (
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
)


def _row_widths(n: int, total: int = 24) -> list[int]:
    """Split total grid units across n tiles as evenly as possible (some tiles get one
    extra unit rather than leaving a ragged gap at the row's end)."""
    base, rem = divmod(total, n)
    return [base + 1 if i < rem else base for i in range(n)]


_TABLE_SQL = _fleet_query("""SELECT
    server_name AS "Server",
    server_id,
    CASE
        WHEN freshness = 'Offline' THEN 'Offline'
        WHEN freshness = 'NeverCollected' THEN 'Awaiting first collection'
        WHEN freshness = 'Stale' THEN 'Stale'
        ELSE 'Online'
    END AS "Status",
    fleet_band AS "Severity",
    cpu_severity AS "CPU",
    threads_severity AS "Threads",
    memory_severity AS "Memory",
    blocking_severity AS "Blocking",
    deadlock_severity AS "Deadlocks",
    collector_severity AS "Collectors",
    last_collection_time AS "Last Collection"
FROM fleet
ORDER BY fleet_score DESC""")

_DRILLDOWN_URL = (
    f"/d/{uid('overview')}?${{__url_time_range}}"
    "&var-server=${__data.fields.server_id}"
)

_CPU_REASON = (
    "CASE WHEN cpu_severity IN ('Critical', 'Warning') "
    "THEN 'CPU ' || ROUND(cpu_total_pct::numeric, 0) || '%' END"
)
_THREADS_REASON = (
    "CASE WHEN threads_severity IN ('Critical', 'Warning') THEN CASE "
    "WHEN starved_requests > 0 THEN 'Threads starved (' || starved_requests || ')' "
    "WHEN runnable_waiting >= 20 THEN 'Threads queued (' || runnable_waiting || ')' "
    "ELSE 'Threads low' END "
    "END"
)
_MEMORY_REASON = (
    "CASE WHEN memory_severity = 'Critical' THEN CASE "
    "WHEN memory_forced_count > 0 THEN 'Memory ' || memory_forced_count || ' forced' "
    "WHEN memory_timeout_count > 0 THEN 'Memory ' || memory_timeout_count || ' timeouts' "
    "WHEN memory_waiter_count > 0 THEN 'Memory ' || memory_waiter_count || ' waiters' "
    "END END"
)
_BLOCKING_REASON = (
    "CASE WHEN blocking_severity IN ('Critical', 'Warning') "
    "THEN 'Blocking ' || blocking_count END"
)
_DEADLOCK_REASON = (
    "CASE WHEN deadlock_severity = 'Critical' THEN 'Deadlocks ' || deadlock_count END"
)
_COLLECTOR_REASON = (
    "CASE WHEN collector_severity = 'Warning' "
    "THEN 'Collectors failing (' || failing_collector_count || ')' END"
)

_STALE_REASON = "CASE WHEN freshness = 'Stale' THEN 'collection stale' END"

_REASON_SQL = f"""CASE
    WHEN freshness = 'Offline' THEN 'Offline - no recent collection'
    WHEN freshness = 'NeverCollected' THEN 'Awaiting first collection'
    ELSE COALESCE(NULLIF(concat_ws(', ', {_CPU_REASON}, {_THREADS_REASON}, {_MEMORY_REASON},
        {_BLOCKING_REASON}, {_DEADLOCK_REASON}, {_COLLECTOR_REASON}, {_STALE_REASON}), ''),
        'Needs attention')
END"""

_CPU_MEM_URL = (
    f"/d/{uid('cpu-memory-sessions')}?${{__url_time_range}}"
    "&var-server=${__data.fields.server_id}"
)
_BLOCKING_URL = (
    f"/d/{uid('blocking-deadlocks')}?${{__url_time_range}}"
    "&var-server=${__data.fields.server_id}"
)
_COLLECTION_URL = (
    f"/d/{uid('collection-health')}?${{__url_time_range}}"
    "&var-server=${__data.fields.server_id}"
)
_QUERIES_URL = (
    f"/d/{uid('queries')}?${{__url_time_range}}"
    "&var-server=${__data.fields.server_id}"
)

_WORST_COUNT = 5

_NEEDS_ATTENTION_SQL = _fleet_query(f"""SELECT
    server_name AS "Server",
    server_id,
    fleet_band AS "Severity",
    {_REASON_SQL} AS "Reason",
    'Open' AS "Quick Links"
FROM fleet
WHERE fleet_band <> 'Healthy'
ORDER BY fleet_score DESC
LIMIT {_WORST_COUNT}""")


def fleet():
    """Build the Fleet dashboard: fleet-wide summary tiles + one sortable per-server table."""
    panels: list[dict] = []

    x = 0
    for (title, expr, th), w in zip(_BAND_TILES, _row_widths(len(_BAND_TILES))):
        sql = _fleet_query(f"SELECT {expr} AS value FROM fleet")
        panels.append(stat(title, x, 0, w, 4, sql, "short", th))
        x += w

    panels.append(
        alertlist(
            "Active Alerts",
            0,
            4,
            8,
            7,
            folder_uid=_ALERT_FOLDER_UID,
            folder_title=_ALERT_FOLDER_TITLE,
            description=(
                "Grafana-managed alert rules currently firing or pending across the fleet "
                f"(folder: {_ALERT_FOLDER_TITLE})."
            ),
        )
    )

    panels.append(
        table(
            "Needs Attention",
            8,
            4,
            16,
            7,
            _NEEDS_ATTENTION_SQL,
            overrides=[
                col_hidden("server_id"),
                col_datalink("Server", "Open Overview", _DRILLDOWN_URL),
                status_colors(
                    "Severity",
                    {"Critical": "red", "Warning": "yellow", "Offline": "text"},
                ),
                col_pills("Reason"),
                col_datalinks(
                    "Quick Links",
                    [
                        ("Open CPU, Memory & Sessions", _CPU_MEM_URL),
                        ("Open Blocking & Deadlocks", _BLOCKING_URL),
                        ("Open Collection Health", _COLLECTION_URL),
                        ("Open Queries", _QUERIES_URL),
                    ],
                ),
            ],
            description=(
                "Worst-first, capped at the "
                f"{_WORST_COUNT} servers needing attention most (see the Critical/Warning "
                "tiles above and the full Fleet health table below for the rest). Empty when "
                "every monitored server is healthy. Click Links to pick a destination dashboard "
                "for the reasons named in this row."
            ),
        )
    )

    indicator_colors = {**HEALTH_STATUS_COLORS, "Healthy": "transparent"}

    overrides = [
        col_hidden("server_id"),
        col_datalink("Server", "Open Overview", _DRILLDOWN_URL),
        status_colors(
            "Status",
            {
                "Online": "green",
                "Stale": "yellow",
                "Offline": "red",
                "Awaiting first collection": "text",
            },
        ),
        status_colors("Severity", {**indicator_colors, "Offline": "text"}),
        status_colors("CPU", indicator_colors),
        col_datalink("CPU", "Open CPU, Memory & Sessions", _CPU_MEM_URL),
        status_colors("Threads", indicator_colors),
        col_datalink("Threads", "Open CPU, Memory & Sessions", _CPU_MEM_URL),
        status_colors("Memory", indicator_colors),
        col_datalink("Memory", "Open CPU, Memory & Sessions", _CPU_MEM_URL),
        status_colors("Blocking", indicator_colors),
        col_datalink("Blocking", "Open Blocking & Deadlocks", _BLOCKING_URL),
        status_colors("Deadlocks", indicator_colors),
        col_datalink("Deadlocks", "Open Blocking & Deadlocks", _BLOCKING_URL),
        status_colors("Collectors", indicator_colors),
        col_datalink("Collectors", "Open Collection Health", _COLLECTION_URL),
    ]

    panels.append(
        table(
            "Fleet health",
            0,
            11,
            24,
            18,
            _TABLE_SQL,
            overrides=overrides,
            description=(
                "One row per monitored server, worst-first. Click a domain indicator to open "
                "the dashboard with that data; the raw numbers behind each indicator live "
                "there, not here. Click a server's name to open its Overview."
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
