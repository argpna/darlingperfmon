"""Waits dashboard (Darling line).

Server Trends only - a foundation proof, not the full parity port.

Upstream ref: WaitsTab (Darling/PerformanceMonitor.Darling.Viewer/).
"""

from ._shared import (
    collector,
    dashboard,
    flow,
    reset_id,
    server_filter,
    server_var,
    subtab,
    table,
    target,
    timeseries,
    uid,
)


def waits():
    """Build the Waits dashboard."""
    reset_id()
    panels: list[dict] = []

    # wait_stats is cumulative since restart - trends must use the delta_* columns.
    wait_ms_sec_sql = f"""
WITH agg AS (
    SELECT
        ws.collection_time,
        ws.server_name,
        SUM(ws.delta_wait_time_ms) AS total_wait_ms
    FROM {collector('wait_stats')} AS ws
    WHERE $__timeFilter(ws.collection_time)
      AND {server_filter('ws.server_id')}
    GROUP BY ws.collection_time, ws.server_name
),
timed AS (
    SELECT
        collection_time,
        server_name,
        total_wait_ms,
        EXTRACT(EPOCH FROM (collection_time
            - LAG(collection_time) OVER (PARTITION BY server_name
                                         ORDER BY collection_time))) AS interval_seconds
    FROM agg
)
SELECT
    collection_time AS time,
    server_name AS metric,
    (total_wait_ms / interval_seconds)::numeric(18, 4) AS wait_ms_sec
FROM timed
WHERE interval_seconds > 0
ORDER BY 1
"""

    signal_vs_resource_sql = f"""
WITH agg AS (
    SELECT
        ws.collection_time,
        SUM(ws.delta_signal_wait_time_ms) AS signal_ms,
        SUM(ws.delta_wait_time_ms) - SUM(ws.delta_signal_wait_time_ms) AS resource_ms
    FROM {collector('wait_stats')} AS ws
    WHERE $__timeFilter(ws.collection_time)
      AND {server_filter('ws.server_id')}
    GROUP BY ws.collection_time
)
SELECT
    collection_time AS time,
    signal_ms AS "Signal (CPU) ms",
    resource_ms AS "Resource ms"
FROM agg
ORDER BY 1
"""

    top_waits_sql = f"""
SELECT
    ws.server_name AS "Server",
    ws.wait_type AS "Wait Type",
    SUM(ws.delta_wait_time_ms) AS "Wait Time (ms)",
    SUM(ws.delta_signal_wait_time_ms) AS "Signal Wait (ms)",
    SUM(ws.delta_waiting_tasks) AS "Waiting Tasks",
    CASE
        WHEN SUM(ws.delta_waiting_tasks) > 0
        THEN (SUM(ws.delta_wait_time_ms)::numeric
              / SUM(ws.delta_waiting_tasks))::numeric(18, 2)
    END AS "Avg Wait (ms)"
FROM {collector('wait_stats')} AS ws
WHERE $__timeFilter(ws.collection_time)
  AND {server_filter('ws.server_id')}
GROUP BY ws.server_name, ws.wait_type
HAVING SUM(ws.delta_wait_time_ms) > 0
ORDER BY 3 DESC
LIMIT 50
"""

    y = subtab(
        panels,
        "Server Trends",
        0,
        [
            (
                12,
                8,
                lambda x, y, w, h: timeseries(
                    "Wait ms/sec", x, y, w, h, [target(wait_ms_sec_sql)], unit="ms"
                ),
            ),
            (
                12,
                8,
                lambda x, y, w, h: timeseries(
                    "Signal vs Resource Waits",
                    x,
                    y,
                    w,
                    h,
                    [target(signal_vs_resource_sql)],
                    unit="ms",
                    stacked=True,
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
                10,
                lambda x, y, w, h: table(
                    "Top Waits",
                    x,
                    y,
                    w,
                    h,
                    top_waits_sql,
                    sort_by=[{"displayName": "Wait Time (ms)", "desc": True}],
                ),
            )
        ],
    )

    return dashboard(uid("waits"), "Waits", panels, [server_var()])
