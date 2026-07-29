"""Session Stats dashboard (Darling line).

Upstream ref: ViewerDataService.SessionStats.cs, SessionStatsChartRenderer and
SessionStatsSummary (PerformanceMonitor.Ui / .Common).
"""

from ._shared import (
    collector,
    dashboard,
    flow,
    reset_id,
    server_filter,
    server_join,
    server_var,
    table,
    target,
    timeseries,
    uid,
)

# Upstream skips a status that is zero across the whole window so all-zero states stay out
# of the legend; the HAVING does the same here.
_COUNTS_SQL = f"""
WITH expanded AS (
    SELECT
        ss.collection_time,
        ss.server_id,
        srv.name AS server_label,
        s.series,
        s.value,
        MAX(s.value) OVER (PARTITION BY ss.server_id, s.series) AS series_max
    FROM {collector('session_summary_stats')} AS ss
    {server_join('ss.server_id')}
    CROSS JOIN LATERAL (VALUES
        ('Total', ss.total_sessions),
        ('Running', ss.running_sessions),
        ('Sleeping', ss.sleeping_sessions),
        ('Background', ss.background_sessions),
        ('Dormant', ss.dormant_sessions),
        ('Idle >30m', ss.idle_sessions_over_30min),
        ('Waiting for Memory', ss.sessions_waiting_for_memory)
    ) AS s(series, value)
    WHERE $__timeFilter(ss.collection_time)
      AND {server_filter('ss.server_id')}
)
SELECT
    collection_time AS time,
    server_label || ' - ' || series AS metric,
    value
FROM expanded
WHERE series_max > 0
ORDER BY 1
"""

# The strip under upstream's chart: attribution from the newest snapshot in the window, the
# values a time series cannot carry.
_SUMMARY_SQL = f"""
SELECT DISTINCT ON (ss.server_id)
    srv.name AS "Server",
    ss.collection_time AS "Collected",
    CASE
        WHEN COALESCE(ss.top_application_name, '') <> ''
        THEN ss.top_application_name
             || ' (' || COALESCE(ss.top_application_connections, 0) || ')'
        ELSE 'N/A'
    END AS "Top Application",
    CASE
        WHEN COALESCE(ss.top_host_name, '') <> ''
        THEN ss.top_host_name || ' (' || COALESCE(ss.top_host_connections, 0) || ')'
        ELSE 'N/A'
    END AS "Top Host",
    ss.databases_with_connections AS "Databases"
FROM {collector('session_summary_stats')} AS ss
{server_join('ss.server_id')}
WHERE $__timeFilter(ss.collection_time)
  AND {server_filter('ss.server_id')}
ORDER BY ss.server_id, ss.collection_time DESC
"""


def session_stats():
    """Build the Session Stats dashboard."""
    reset_id()
    panels: list[dict] = []

    flow(
        panels,
        0,
        [
            (
                24,
                12,
                lambda x, y, w, h: timeseries(
                    "Session Counts Over Time",
                    x,
                    y,
                    w,
                    h,
                    [target(_COUNTS_SQL)],
                    axis_label="Session Count",
                ),
            ),
            (
                24,
                5,
                lambda x, y, w, h: table(
                    "Session Attribution",
                    x,
                    y,
                    w,
                    h,
                    _SUMMARY_SQL,
                ),
            ),
        ],
    )

    return dashboard(uid("session-stats"), "Session Stats", panels, [server_var()])
