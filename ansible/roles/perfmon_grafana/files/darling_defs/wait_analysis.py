"""Wait Analysis dashboard - merges Wait Stats and Latches &
Spinlocks dashboards: three angles ("what is the engine waiting on") on one page.

Upstream ref: ViewerServerTab.Waits.cs / ViewerDataService.Waits.cs,
ViewerDataService.LatchSpinlock.cs / ViewerServerTab.LatchSpinlock.cs. The Wait Stats
section's own module (waits.py) still carries the $wait_type-scoped SQL and the Wait
Drill-Down nav-only dashboard it links to; this module composes that section with the
Latches & Spinlocks section it never needed to share anything with.
"""

from ._shared import (
    collector,
    dashboard,
    flow,
    reset_id,
    server_filter,
    server_var,
    stat_grid,
    subtab,
    table,
    target,
    thresholds,
    timeseries,
    uid,
    UTC_NOW,
)
from .waits import wait_stats_section, wait_type_var


def _rate_sql(base: str, dimension: str, delta: str) -> str:
    """Per-second rate for the five heaviest classes, on upstream's truncate-then-diff idiom.

    $server is single-select, so the legend is just the class - no server label needed.
    """
    return f"""
WITH top_classes AS (
    SELECT t.server_id, t.{dimension}
    FROM {collector(base)} AS t
    WHERE $__timeFilter(t.collection_time)
      AND {server_filter('t.server_id')}
    GROUP BY t.server_id, t.{dimension}
    ORDER BY SUM(t.{delta}) DESC
    LIMIT 5
),
windowed AS (
    SELECT
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
    {dimension} AS metric,
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
    SELECT t.server_id, MAX(t.collection_time) AS mx
    FROM {collector(base)} AS t
    WHERE $__timeFilter(t.collection_time)
      AND {server_filter('t.server_id')}
    GROUP BY t.server_id
)
SELECT
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

# Stat row: top wait type, total wait ms/sec, latch wait count, spinlock collision rate.
# A short trailing window, not $__timeFilter - "right now" snapshot tiles, matching
# Overview's and Collection Health's stat rows.
_TOP_WAIT_TYPE_SQL = f"""
SELECT wait_type AS v
FROM {collector('wait_stats')}
WHERE collection_time >= {UTC_NOW} - INTERVAL '1 hour' AND {server_filter()}
GROUP BY wait_type
ORDER BY SUM(delta_wait_time_ms) DESC
LIMIT 1
"""

_TOTAL_WAIT_MS_SEC_SQL = f"""
SELECT CASE WHEN SUM(interval_seconds) > 0
    THEN SUM(delta_wait_time_ms)::double precision / SUM(interval_seconds)
    ELSE 0 END AS v
FROM (
    SELECT
        delta_wait_time_ms,
        EXTRACT(EPOCH FROM (date_trunc('second', collection_time)
            - date_trunc('second', LAG(collection_time) OVER (ORDER BY collection_time))))
            AS interval_seconds
    FROM {collector('wait_stats')}
    WHERE collection_time >= {UTC_NOW} - INTERVAL '1 hour' AND {server_filter()}
) AS w
"""

_LATCH_WAIT_COUNT_SQL = f"""
SELECT COALESCE(SUM(delta_waiting_requests_count), 0) AS v
FROM {collector('latch_stats')}
WHERE collection_time >= {UTC_NOW} - INTERVAL '1 hour' AND {server_filter()}
"""

_SPINLOCK_COLLISION_RATE_SQL = f"""
SELECT CASE WHEN SUM(interval_seconds) > 0
    THEN SUM(delta_collisions)::double precision / SUM(interval_seconds)
    ELSE 0 END AS v
FROM (
    SELECT
        delta_collisions,
        EXTRACT(EPOCH FROM (date_trunc('second', collection_time)
            - date_trunc('second', LAG(collection_time) OVER (ORDER BY collection_time))))
            AS interval_seconds
    FROM {collector('spinlock_stats')}
    WHERE collection_time >= {UTC_NOW} - INTERVAL '1 hour' AND {server_filter()}
) AS s
"""

_STAT_ROW = [
    {
        "title": "Top Wait Type",
        "sql": _TOP_WAIT_TYPE_SQL,
        "th": thresholds(("text", None)),
        "fields": "/.*/",
    },
    {
        "title": "Total Wait ms/sec",
        "sql": _TOTAL_WAIT_MS_SEC_SQL,
        "th": thresholds(("text", None)),
    },
    {
        "title": "Latch Wait Count",
        "sql": _LATCH_WAIT_COUNT_SQL,
        "th": thresholds(("text", None)),
    },
    {
        "title": "Spinlock Collision Rate",
        "sql": _SPINLOCK_COLLISION_RATE_SQL,
        "th": thresholds(("text", None)),
    },
]


def _latch_spinlock_section(panels: list[dict], y: int) -> int:
    """Append the Latches & Spinlocks section - already a clean 2x2 grid, unchanged."""
    return subtab(
        panels,
        "Latches & Spinlocks",
        y,
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
                    "Latch Stats", x, y, w, h, _LATCH_SNAPSHOT_SQL
                ),
            ),
            (
                12,
                10,
                lambda x, y, w, h: table(
                    "Spinlock Stats", x, y, w, h, _SPINLOCK_SNAPSHOT_SQL
                ),
            ),
        ],
    )


def wait_analysis():
    """Build the Wait Analysis dashboard: status stat row, Wait Stats, Latches & Spinlocks."""
    reset_id()
    panels: list[dict] = []

    y = flow(panels, 0, [(24, 4, stat_grid(_STAT_ROW, cols=4))])
    y = wait_stats_section(panels, y)
    y = _latch_spinlock_section(panels, y)

    return dashboard(
        uid("wait-analysis"),
        "Wait Analysis",
        panels,
        [server_var(), wait_type_var()],
    )
