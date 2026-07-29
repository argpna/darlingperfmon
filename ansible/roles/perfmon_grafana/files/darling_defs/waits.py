"""Wait Stats dashboard (Darling line).

Upstream ref: ViewerServerTab.Waits.cs / ViewerDataService.Waits.cs.

The upstream tab is a wait-type picker driving one chart with a two-option metric combo.
The picker becomes $wait_type; the combo's options become their own panels, since a Grafana
panel has no combo and showing both costs nothing.
"""

from ._shared import (
    collector,
    dashboard,
    flow,
    multi_filter,
    query_var,
    reset_id,
    server_filter,
    server_join,
    server_var,
    target,
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

    interval_seconds is the truncate-then-diff epoch idiom upstream uses, and the LAG is
    partitioned per server so one server's gap cannot skew another's rate.
    """
    return f"""
WITH ranked AS (
    SELECT
        ws.server_id,
        srv.name AS server_label,
        ws.wait_type
    FROM {collector('wait_stats')} AS ws
    {server_join('ws.server_id')}
    WHERE $__timeFilter(ws.collection_time)
      AND {server_filter('ws.server_id')}
      AND {multi_filter('ws.wait_type', 'wait_type')}
    GROUP BY ws.server_id, srv.name, ws.wait_type
    ORDER BY SUM(ws.delta_wait_time_ms) DESC
    LIMIT 20
),
windowed AS (
    SELECT
        r.server_label,
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
    server_label || ' - ' || wait_type AS metric,
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


def waits():
    """Build the Wait Stats dashboard."""
    reset_id()
    panels: list[dict] = []

    flow(
        panels,
        0,
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
        ],
    )

    wait_type_var = query_var(
        "wait_type",
        "Wait type",
        _WAIT_TYPES_QUERY,
        "Wait types collected over the window, heaviest first. All plots the top 20.",
    )

    return dashboard(uid("waits"), "Wait Stats", panels, [server_var(), wait_type_var])
