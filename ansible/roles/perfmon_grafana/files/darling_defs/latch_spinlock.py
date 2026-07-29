"""Latches & Spinlocks dashboard (Darling line).

Upstream ref: ViewerDataService.LatchSpinlock.cs, ViewerServerTab.LatchSpinlock.cs.
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


def _rate_sql(base: str, dimension: str, delta: str) -> str:
    """Per-second rate for the five heaviest classes, on upstream's truncate-then-diff idiom."""
    return f"""
WITH top_classes AS (
    SELECT
        t.server_id,
        srv.name AS server_label,
        t.{dimension}
    FROM {collector(base)} AS t
    {server_join('t.server_id')}
    WHERE $__timeFilter(t.collection_time)
      AND {server_filter('t.server_id')}
    GROUP BY t.server_id, srv.name, t.{dimension}
    ORDER BY SUM(t.{delta}) DESC
    LIMIT 5
),
windowed AS (
    SELECT
        c.server_label,
        t.{dimension},
        t.collection_time,
        t.{delta} AS delta_value,
        EXTRACT(EPOCH FROM (date_trunc('second', t.collection_time)
            - date_trunc('second', LAG(t.collection_time) OVER (
                PARTITION BY t.server_id, t.{dimension}
                ORDER BY t.collection_time)))) AS interval_seconds
    FROM {collector(base)} AS t
    JOIN top_classes AS c
      ON c.server_id = t.server_id
     AND c.{dimension} = t.{dimension}
    WHERE $__timeFilter(t.collection_time)
      AND {server_filter('t.server_id')}
)
SELECT
    collection_time AS time,
    server_label || ' - ' || {dimension} AS metric,
    CASE
        WHEN interval_seconds > 0
        THEN delta_value::double precision / interval_seconds
        ELSE 0
    END AS value
FROM windowed
ORDER BY 1
"""


def _snapshot_sql(base: str, columns: str, order: str) -> str:
    """The newest collection's rows, top 20, as upstream's latest-snapshot grid."""
    return f"""
WITH latest AS (
    SELECT
        t.server_id,
        srv.name AS server_label,
        MAX(t.collection_time) AS mx
    FROM {collector(base)} AS t
    {server_join('t.server_id')}
    WHERE $__timeFilter(t.collection_time)
      AND {server_filter('t.server_id')}
    GROUP BY t.server_id, srv.name
)
SELECT
    l.server_label AS "Server",
{columns}
FROM latest AS l
JOIN {collector(base)} AS t
  ON t.server_id = l.server_id
 AND t.collection_time = l.mx
ORDER BY {order}
LIMIT 20
"""


_LATCH_TREND_SQL = _rate_sql("latch_stats", "latch_class", "delta_wait_time_ms")

_LATCH_SNAPSHOT_SQL = _snapshot_sql(
    "latch_stats",
    """    t.latch_class AS "Latch Class",
    t.waiting_requests_count AS "Waiting Requests",
    t.wait_time_ms AS "Wait Time (ms)",
    t.max_wait_time_ms AS "Max Wait Time (ms)",
    t.delta_waiting_requests_count AS "Waiting Requests (delta)",
    t.delta_wait_time_ms AS "Wait Time (delta ms)\"""",
    "t.delta_wait_time_ms DESC, t.wait_time_ms DESC",
)

_SPINLOCK_TREND_SQL = _rate_sql("spinlock_stats", "spinlock_name", "delta_collisions")

_SPINLOCK_SNAPSHOT_SQL = _snapshot_sql(
    "spinlock_stats",
    """    t.spinlock_name AS "Spinlock",
    t.collisions AS "Collisions",
    t.spins AS "Spins",
    t.spins_per_collision AS "Spins per Collision",
    t.sleep_time AS "Sleep Time",
    t.backoffs AS "Backoffs",
    t.delta_collisions AS "Collisions (delta)",
    t.delta_spins AS "Spins (delta)\"""",
    "t.delta_collisions DESC, t.collisions DESC",
)


def latch_spinlock():
    """Build the Latches & Spinlocks dashboard."""
    reset_id()
    panels: list[dict] = []

    flow(
        panels,
        0,
        [
            (
                12,
                9,
                lambda x, y, w, h: timeseries(
                    "Latch Waits",
                    x,
                    y,
                    w,
                    h,
                    [target(_LATCH_TREND_SQL)],
                    unit="ms",
                    axis_label="Wait Time (ms/sec)",
                ),
            ),
            (
                12,
                9,
                lambda x, y, w, h: timeseries(
                    "Spinlock Collisions",
                    x,
                    y,
                    w,
                    h,
                    [target(_SPINLOCK_TREND_SQL)],
                    axis_label="Collisions/sec",
                ),
            ),
            (
                12,
                10,
                lambda x, y, w, h: table(
                    "Latch Stats",
                    x,
                    y,
                    w,
                    h,
                    _LATCH_SNAPSHOT_SQL,
                ),
            ),
            (
                12,
                10,
                lambda x, y, w, h: table(
                    "Spinlock Stats",
                    x,
                    y,
                    w,
                    h,
                    _SPINLOCK_SNAPSHOT_SQL,
                ),
            ),
        ],
    )

    return dashboard(
        uid("latch-spinlock"), "Latches & Spinlocks", panels, [server_var()]
    )
