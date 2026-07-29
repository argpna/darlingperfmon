"""Daily Summary dashboard (Darling line).

Upstream ref: DailySummarySql.RangeSql / RangeSqlFor (Darling.Storage),
ViewerServerTab.DailySummary.cs, DailyHealthBandCalculator (PerformanceMonitor.Common).

Upstream renders a month heatmap of composite daily health and opens a day-detail panel on
click. Grafana has no month-grid calendar, so the heatmap becomes a state timeline banded by
the same composite verdict, and the day detail becomes a row per day in the table below it -
every signal the detail panel shows is a column, visible for all days at once instead of one
at a time.
"""

from ._shared import (
    HEALTH_STATUS_COLORS,
    collector,
    col_thresholds,
    col_unit,
    dashboard,
    duration_ms,
    flow,
    n0,
    reset_id,
    rollup,
    server_filter,
    server_join,
    server_var,
    state_timeline,
    status_colors,
    table,
    tiered,
    uid,
)

# DailyHealthThresholds.Default. Named here for the same reason upstream names them: this is
# the one place to retune how red or green a month reads.
_HIGH_CPU_CRITICAL = 6
_HIGH_CPU_WARNING = 1
_BLOCKING_CRITICAL = 11
_BLOCKING_WARNING = 1
_ALERT_WARNING = 1

# The DailyHealthBand enum values, so the calendar's numeric bands read as upstream's.
# NoData has no branch: every row the aggregate returns came off the day spine, so upstream
# sets HasData true for all of them and a day with no collection is simply absent.
_STATES = [(1, "Healthy", "green"), (2, "Warning", "yellow"), (3, "Critical", "red")]

# Upstream ref: DailyHealthBandCalculator.Classify - severity is first-match-wins.
_BAND_LEVEL = f"""CASE
        WHEN d.deadlock_count > 0
          OR d.collection_errors > 0
          OR d.memory_critical_events > 0
          OR d.high_cpu_events >= {_HIGH_CPU_CRITICAL}
          OR d.blocking_events >= {_BLOCKING_CRITICAL}
        THEN 3
        WHEN d.high_cpu_events >= {_HIGH_CPU_WARNING}
          OR d.blocking_events >= {_BLOCKING_WARNING}
          OR d.memory_pressure_events > 0
          OR d.alert_count >= {_ALERT_WARNING}
        THEN 2
        ELSE 1
    END"""

# DailyHealthBandCalculator.Label, off the same level so the two cannot disagree.
_BAND_LABEL = (
    f"CASE {_BAND_LEVEL} "
    + " ".join(f"WHEN {value} THEN '{text}'" for value, text, _ in _STATES)
    + " END"
)


def _count_line(expr: str, singular: str, plural: str) -> str:
    """One BuildSignalLines entry: NULL when the signal is zero, so concat_ws drops it."""
    return f"""CASE WHEN {expr} > 0
        THEN {n0(expr)} || ' ' || CASE WHEN {expr} = 1 THEN '{singular}' ELSE '{plural}' END
    END"""


# The blocking line carries the day's peak block duration, as the day-detail panel does.
_BLOCKING_LINE = f"""CASE WHEN d.blocking_events > 0
        THEN {n0('d.blocking_events')}
             || CASE WHEN d.blocking_events = 1 THEN ' blocking event' ELSE ' blocking events' END
             || CASE WHEN d.peak_block_wait_ms > 0
                     THEN ' (peak block ' || {duration_ms('d.peak_block_wait_ms')} || ')'
                     ELSE '' END
    END"""

# MemoryPressureEvents is the full count; the severe subset is listed on its own line, so
# only the remainder goes here (upstream avoids the same double-count).
_REASONS = f"""COALESCE(NULLIF(concat_ws(', ',
        {_count_line('d.deadlock_count', 'deadlock', 'deadlocks')},
        {_count_line('d.collection_errors', 'collection error', 'collection errors')},
        {_count_line('d.high_cpu_events', 'high-CPU sample', 'high-CPU samples')},
        {_BLOCKING_LINE},
        {_count_line('d.memory_critical_events',
                     'severe memory-pressure event', 'severe memory-pressure events')},
        {_count_line('GREATEST(0, d.memory_pressure_events - d.memory_critical_events)',
                     'memory-pressure event', 'memory-pressure events')},
        {_count_line('d.alert_count', 'alert', 'alerts')}
    ), ''), 'No issues detected.')"""


def _queries_cte(relation: str, time_col: str) -> str:
    """The one CTE that reads a table with a raw horizon shorter than its rollups reach.

    COUNT(DISTINCT query_hash) is exact over a CAGG because query_hash is one of its GROUP BY
    columns. Upstream ref: DailySummarySql.QueriesCteForCagg (#1661).
    """
    return f"""
    SELECT server_id, date_trunc('day', {time_col}) AS d,
           COUNT(DISTINCT query_hash) AS c
    FROM {relation}
    WHERE $__timeFilter({time_col}) AND {server_filter()}
    GROUP BY 1, 2
"""


def _range_sql(queries_cte: str) -> str:
    """The whole daily aggregate for one retention tier.

    Upstream runs this per server; $server is multi-select here, so server_id joins every CTE
    and the day spine, giving one row per (server, day).
    """
    return f"""
WITH wait_per_type AS (
    SELECT ws.server_id, date_trunc('day', ws.collection_time) AS d, ws.wait_type,
           SUM(ws.delta_wait_time_ms) AS ms
    FROM {collector('wait_stats')} AS ws
    WHERE $__timeFilter(ws.collection_time)
      AND {server_filter('ws.server_id')}
      AND ws.delta_wait_time_ms > 0
    GROUP BY 1, 2, 3
),
wait_totals AS (
    SELECT server_id, d, SUM(ms) / 1000.0 AS total_wait_sec
    FROM wait_per_type GROUP BY 1, 2
),
wait_top AS (
    /* Per-day top wait type = the wait with the most delta time that day. */
    SELECT DISTINCT ON (server_id, d) server_id, d, wait_type AS top_wait_type
    FROM wait_per_type ORDER BY server_id, d, ms DESC
),
waits AS (
    SELECT t.server_id, t.d, t.total_wait_sec, tp.top_wait_type
    FROM wait_totals t
    LEFT JOIN wait_top tp ON tp.server_id = t.server_id AND tp.d = t.d
),
queries AS ({queries_cte}),
deadlocks AS (
    SELECT server_id, date_trunc('day', collection_time) AS d, COUNT(*) AS c
    FROM {collector('deadlocks')}
    WHERE $__timeFilter(collection_time) AND {server_filter()}
    GROUP BY 1, 2
),
bpr AS (
    SELECT server_id, date_trunc('day', collection_time) AS d,
           COUNT(*) AS c, MAX(wait_time_ms) AS max_wait_ms
    FROM {collector('blocked_process_reports')}
    WHERE $__timeFilter(collection_time) AND {server_filter()}
    GROUP BY 1, 2
),
dmv AS (
    SELECT server_id, date_trunc('day', collection_time) AS d,
           COUNT(*) AS c, MAX(wait_time_ms) AS max_wait_ms
    FROM {collector('dmv_blocking_snapshots')}
    WHERE $__timeFilter(collection_time) AND {server_filter()}
    GROUP BY 1, 2
),
cpu AS (
    /* Total host CPU = SQL + other-process (NULL on Linux -> 0), matching the alert engine;
       sustained >= 80 samples drive the day's band. */
    SELECT server_id, date_trunc('day', collection_time) AS d,
           COUNT(*) FILTER (
               WHERE (sqlserver_cpu_utilization
                      + COALESCE(other_process_cpu_utilization, 0)) >= 80) AS c
    FROM {collector('cpu_utilization_stats')}
    WHERE $__timeFilter(collection_time) AND {server_filter()}
    GROUP BY 1, 2
),
coll AS (
    /* Any run marks the day as collected, so a quiet monitored day still bands Healthy. */
    SELECT server_id, date_trunc('day', collection_time) AS d,
           COUNT(*) AS runs,
           COUNT(*) FILTER (WHERE status = 'ERROR') AS errs
    FROM {collector('collection_log')}
    WHERE $__timeFilter(collection_time) AND {server_filter()}
    GROUP BY 1, 2
),
mem AS (
    SELECT server_id, date_trunc('day', collection_time) AS d,
           COUNT(*) FILTER (
               WHERE memory_indicators_process >= 2
                  OR memory_indicators_system >= 2) AS pressure,
           COUNT(*) FILTER (WHERE memory_indicators_process >= 3) AS critical
    FROM {collector('memory_pressure_events')}
    WHERE $__timeFilter(collection_time) AND {server_filter()}
    GROUP BY 1, 2
),
alerts AS (
    /* Actionable alerts only: no dismissed rows, no resolution notices. The suffix list
       mirrors AlertMetricClassifier.IsResolution and must match it exactly - widening one
       without the other counts recoveries as actionable alerts. */
    SELECT server_id, date_trunc('day', alert_time) AS d, COUNT(*) AS c
    FROM config.config_alert_log
    WHERE $__timeFilter(alert_time)
      AND {server_filter()}
      AND dismissed = FALSE
      AND metric_name NOT LIKE '%Cleared%'
      AND metric_name NOT LIKE '%Resolved%'
      AND metric_name NOT LIKE '%Restored%'
      AND metric_name NOT LIKE '%Resumed%'
      AND metric_name NOT LIKE '%Restarted%'
      AND metric_name NOT LIKE '%Recovered%'
      AND metric_name NOT LIKE '%Reconnected%'
    GROUP BY 1, 2
),
day_spine AS (
    SELECT server_id, d FROM waits
    UNION SELECT server_id, d FROM queries
    UNION SELECT server_id, d FROM deadlocks
    UNION SELECT server_id, d FROM bpr
    UNION SELECT server_id, d FROM dmv
    UNION SELECT server_id, d FROM cpu
    UNION SELECT server_id, d FROM coll
    UNION SELECT server_id, d FROM mem
    UNION SELECT server_id, d FROM alerts
)
SELECT
    s.d AS day,
    s.server_id,
    COALESCE(w.total_wait_sec, 0) AS total_wait_sec,
    w.top_wait_type,
    COALESCE(q.c, 0) AS unique_queries,
    COALESCE(dl.c, 0) AS deadlock_count,
    COALESCE(NULLIF(b.c, 0), dm.c, 0) AS blocking_events,
    COALESCE(cp.c, 0) AS high_cpu_events,
    COALESCE(cl.errs, 0) AS collection_errors,
    COALESCE(m.pressure, 0) AS memory_pressure_events,
    COALESCE(m.critical, 0) AS memory_critical_events,
    COALESCE(al.c, 0) AS alert_count,
    /* Peak block wait from the SAME source the count came from (BPR preferred, DMV-snapshot
       fallback), so the blocking reason reconciles with the count. */
    COALESCE(CASE WHEN COALESCE(b.c, 0) > 0 THEN b.max_wait_ms ELSE dm.max_wait_ms END, 0)
        AS peak_block_wait_ms
FROM day_spine s
LEFT JOIN waits w ON w.server_id = s.server_id AND w.d = s.d
LEFT JOIN queries q ON q.server_id = s.server_id AND q.d = s.d
LEFT JOIN deadlocks dl ON dl.server_id = s.server_id AND dl.d = s.d
LEFT JOIN bpr b ON b.server_id = s.server_id AND b.d = s.d
LEFT JOIN dmv dm ON dm.server_id = s.server_id AND dm.d = s.d
LEFT JOIN cpu cp ON cp.server_id = s.server_id AND cp.d = s.d
LEFT JOIN coll cl ON cl.server_id = s.server_id AND cl.d = s.d
LEFT JOIN mem m ON m.server_id = s.server_id AND m.d = s.d
LEFT JOIN alerts al ON al.server_id = s.server_id AND al.d = s.d
"""


# Only the queries CTE changes per tier, exactly as RangeSqlFor swaps it.
_DAILY = tiered(
    {
        "raw": _range_sql(_queries_cte(collector("query_stats"), "collection_time")),
        "hourly": _range_sql(_queries_cte(rollup("query_stats", "hourly"), "bucket")),
        "daily": _range_sql(_queries_cte(rollup("query_stats", "daily"), "bucket")),
    },
    base="query_stats",
)

_CALENDAR_SQL = f"""
WITH d AS ({_DAILY})
SELECT d.day AS time, srv.name AS metric, {_BAND_LEVEL} AS band
FROM d
{server_join('d.server_id')}
ORDER BY 1
"""

_DETAIL_SQL = f"""
WITH d AS ({_DAILY})
SELECT
    d.day AS "Day",
    srv.name AS "Server",
    {_BAND_LABEL} AS "Health",
    {_REASONS} AS "Why",
    COALESCE(NULLIF(d.top_wait_type, ''), 'none') AS "Top Wait",
    d.total_wait_sec AS "Total Wait",
    d.unique_queries AS "Unique Queries",
    d.high_cpu_events AS "High-CPU Samples",
    d.deadlock_count AS "Deadlocks",
    d.blocking_events AS "Blocking Events",
    d.peak_block_wait_ms AS "Peak Block",
    d.memory_pressure_events AS "Memory Pressure",
    d.memory_critical_events AS "Severe Memory",
    d.collection_errors AS "Collection Errors",
    d.alert_count AS "Alerts"
FROM d
{server_join('d.server_id')}
ORDER BY d.day DESC, srv.name
"""


def daily_summary():
    """Build the Daily Summary dashboard."""
    reset_id()
    panels: list[dict] = []

    flow(
        panels,
        0,
        [
            (
                24,
                7,
                lambda x, y, w, h: state_timeline(
                    "Performance Calendar",
                    x,
                    y,
                    w,
                    h,
                    _CALENDAR_SQL,
                    _STATES,
                    description=(
                        "Composite daily health, one band per day. A day with no "
                        "collection at all is absent rather than banded."
                    ),
                ),
            ),
            (
                24,
                14,
                lambda x, y, w, h: table(
                    "Daily Detail",
                    x,
                    y,
                    w,
                    h,
                    _DETAIL_SQL,
                    overrides=[
                        status_colors("Health", HEALTH_STATUS_COLORS),
                        col_unit("Total Wait", "s"),
                        col_unit("Peak Block", "ms"),
                        col_thresholds("Deadlocks", ("text", None), ("red", 1)),
                        col_thresholds("Collection Errors", ("text", None), ("red", 1)),
                        col_thresholds(
                            "High-CPU Samples",
                            ("text", None),
                            ("yellow", _HIGH_CPU_WARNING),
                            ("red", _HIGH_CPU_CRITICAL),
                        ),
                        col_thresholds(
                            "Blocking Events",
                            ("text", None),
                            ("yellow", _BLOCKING_WARNING),
                            ("red", _BLOCKING_CRITICAL),
                        ),
                        col_thresholds("Severe Memory", ("text", None), ("red", 1)),
                    ],
                    sort_by=[{"displayName": "Day", "desc": True}],
                ),
            ),
        ],
    )

    return dashboard(
        uid("daily-summary"),
        "Daily Summary",
        panels,
        [server_var()],
        time_from="now-30d",
        refresh="5m",
    )
