"""Memory dashboard (Darling line).

Upstream ref: ViewerDataService.Memory.cs, ViewerDataService.PlanCache.cs,
MemoryTrendSql (ViewerDataService.OverviewLanes.cs), ViewerServerTab.Memory.cs.
"""

from ._shared import (
    col_unit,
    collector,
    dashboard,
    multi_filter,
    query_var,
    reset_id,
    server_filter,
    server_join,
    server_var,
    subtab,
    table,
    target,
    timeseries,
    uid,
)

# Upstream's summary strip reads the newest row with no window at all - it is current
# state, not a view of the selected range.
_SUMMARY_SQL = f"""
SELECT DISTINCT ON (ms.server_id)
    srv.name AS "Server",
    ms.collection_time AS "Collected",
    ms.total_physical_memory_mb AS "Physical Memory",
    ms.total_server_memory_mb AS "SQL Server Memory",
    ms.target_server_memory_mb AS "Target Memory",
    ms.buffer_pool_mb AS "Buffer Pool",
    ms.plan_cache_mb AS "Plan Cache",
    ms.available_physical_memory_mb AS "Available Physical",
    ms.total_page_file_mb AS "Total Page File",
    ms.available_page_file_mb AS "Available Page File",
    ms.system_memory_state AS "System Memory State",
    ms.sql_memory_model AS "Memory Model"
FROM {collector('memory_stats')} AS ms
{server_join('ms.server_id')}
WHERE {server_filter('ms.server_id')}
ORDER BY ms.server_id, ms.collection_time DESC
"""

# Memory grants come from their own collector, so the overlay is a second branch unioned
# onto the memory_stats series rather than another column.
_MEMORY_TREND_SQL = f"""
SELECT
    ms.collection_time AS time,
    srv.name || ' - ' || s.series AS metric,
    s.value / 1024.0 AS value
FROM {collector('memory_stats')} AS ms
{server_join('ms.server_id')}
CROSS JOIN LATERAL (VALUES
    ('Total Server Memory', ms.total_server_memory_mb),
    ('Target Memory', ms.target_server_memory_mb),
    ('Buffer Pool', ms.buffer_pool_mb)
) AS s(series, value)
WHERE $__timeFilter(ms.collection_time)
  AND {server_filter('ms.server_id')}
UNION ALL
SELECT
    mg.collection_time AS time,
    srv.name || ' - Memory Grants' AS metric,
    SUM(mg.granted_memory_mb) / 1024.0 AS value
FROM {collector('memory_grant_stats')} AS mg
{server_join('mg.server_id')}
WHERE $__timeFilter(mg.collection_time)
  AND {server_filter('mg.server_id')}
GROUP BY mg.collection_time, srv.name
ORDER BY 1
"""

_CLERK_TYPES_QUERY = f"""
SELECT mc.clerk_type
FROM {collector('memory_clerks')} AS mc
WHERE $__timeFilter(mc.collection_time)
  AND {server_filter('mc.server_id')}
GROUP BY mc.clerk_type
ORDER BY SUM(mc.memory_mb) DESC
"""

# Upstream's picker pre-selects the five heaviest clerks; All means the same five here.
_CLERK_RANKED = f"""
    SELECT
        mc.server_id,
        srv.name AS server_label,
        mc.clerk_type
    FROM {collector('memory_clerks')} AS mc
    {server_join('mc.server_id')}
    WHERE $__timeFilter(mc.collection_time)
      AND {server_filter('mc.server_id')}
      AND {multi_filter('mc.clerk_type', 'clerk_type')}
    GROUP BY mc.server_id, srv.name, mc.clerk_type
    ORDER BY SUM(mc.memory_mb) DESC
    LIMIT 5
"""

_CLERK_TREND_SQL = f"""
WITH ranked AS (
{_CLERK_RANKED}
)
SELECT
    mc.collection_time AS time,
    r.server_label || ' - ' || mc.clerk_type AS metric,
    mc.memory_mb AS value
FROM {collector('memory_clerks')} AS mc
JOIN ranked AS r
  ON r.server_id = mc.server_id
 AND r.clerk_type = mc.clerk_type
WHERE $__timeFilter(mc.collection_time)
  AND {server_filter('mc.server_id')}
ORDER BY 1
"""

# The strip under upstream's clerk chart: totals over the selected clerks' latest values,
# buffer pool excluded, and the heaviest of those clerks named.
_CLERK_SUMMARY_SQL = f"""
WITH ranked AS (
{_CLERK_RANKED}
),
latest AS (
    SELECT DISTINCT ON (mc.server_id, mc.clerk_type)
        r.server_label,
        mc.clerk_type,
        mc.memory_mb
    FROM {collector('memory_clerks')} AS mc
    JOIN ranked AS r
      ON r.server_id = mc.server_id
     AND r.clerk_type = mc.clerk_type
    WHERE $__timeFilter(mc.collection_time)
      AND {server_filter('mc.server_id')}
    ORDER BY mc.server_id, mc.clerk_type, mc.collection_time DESC
),
non_bp AS (
    SELECT *
    FROM latest
    WHERE clerk_type NOT ILIKE '%BUFFERPOOL%'
)
SELECT DISTINCT ON (n.server_label)
    n.server_label AS "Server",
    SUM(n.memory_mb) OVER (PARTITION BY n.server_label) AS "Non-BP Total (MB)",
    regexp_replace(n.clerk_type, '^MEMORYCLERK_', '') AS "Top Non-BP Clerk",
    n.memory_mb AS "Top Non-BP Clerk (MB)"
FROM non_bp AS n
ORDER BY n.server_label, n.memory_mb DESC
"""


def _grant_sql(metrics: str) -> str:
    """Per-pool grant series, one metric per LATERAL row as upstream charts them."""
    return f"""
SELECT
    mg.collection_time AS time,
    srv.name || ' - Pool ' || mg.pool_id || ': ' || s.series AS metric,
    SUM(s.value) AS value
FROM {collector('memory_grant_stats')} AS mg
{server_join('mg.server_id')}
CROSS JOIN LATERAL (VALUES
    {metrics}
) AS s(series, value)
WHERE $__timeFilter(mg.collection_time)
  AND {server_filter('mg.server_id')}
GROUP BY mg.collection_time, srv.name, mg.pool_id, s.series
ORDER BY 1
"""


_GRANT_SIZING_SQL = _grant_sql("""('Available MB', mg.available_memory_mb),
    ('Granted MB', mg.granted_memory_mb),
    ('Used MB', mg.used_memory_mb),
    ('Target MB', mg.target_memory_mb),
    ('Max Target MB', mg.max_target_memory_mb)""")

_GRANT_ACTIVITY_SQL = _grant_sql("""('Grantees', mg.grantee_count),
    ('Waiters', mg.waiter_count),
    ('Timeouts', mg.timeout_error_count_delta),
    ('Forced Grants', mg.forced_grant_count_delta)""")

_PLAN_CACHE_TREND_SQL = f"""
SELECT
    pc.collection_time AS time,
    srv.name || ' - ' || s.series AS metric,
    SUM(s.value) AS value
FROM {collector('plan_cache_stats')} AS pc
{server_join('pc.server_id')}
CROSS JOIN LATERAL (VALUES
    ('Single-Use', pc.single_use_size_mb),
    ('Multi-Use', pc.multi_use_size_mb)
) AS s(series, value)
WHERE $__timeFilter(pc.collection_time)
  AND {server_filter('pc.server_id')}
GROUP BY pc.collection_time, srv.name, s.series
ORDER BY 1
"""

# Upstream ref: ClassifyPlanCacheBloat - single-use plans over total plans.
_PLAN_CACHE_SUMMARY_SQL = f"""
WITH latest AS (
    SELECT
        pc.server_id,
        srv.name AS server_label,
        MAX(pc.collection_time) AS mx
    FROM {collector('plan_cache_stats')} AS pc
    {server_join('pc.server_id')}
    WHERE $__timeFilter(pc.collection_time)
      AND {server_filter('pc.server_id')}
    GROUP BY pc.server_id, srv.name
),
totals AS (
    SELECT
        l.server_label,
        COALESCE(SUM(pc.total_plans), 0) AS total_plans,
        COALESCE(SUM(pc.single_use_plans), 0) AS single_use_plans,
        MIN(pc.oldest_plan_create_time) AS oldest_plan_create_time
    FROM latest AS l
    JOIN {collector('plan_cache_stats')} AS pc
      ON pc.server_id = l.server_id
     AND pc.collection_time = l.mx
    GROUP BY l.server_label
),
classified AS (
    SELECT
        t.*,
        CASE
            WHEN t.total_plans > 0
            THEN t.single_use_plans * 100.0 / t.total_plans
            ELSE 0
        END AS single_use_percent
    FROM totals AS t
)
SELECT
    c.server_label AS "Server",
    c.total_plans AS "Total Plans",
    c.oldest_plan_create_time AS "Oldest Plan",
    CASE
        WHEN c.single_use_percent > 50 THEN 'CRITICAL'
        WHEN c.single_use_percent > 30 THEN 'HIGH'
        WHEN c.single_use_percent > 20 THEN 'MEDIUM'
        ELSE 'NORMAL'
    END AS "Bloat Level",
    CASE
        WHEN c.single_use_percent > 20
        THEN 'Check for unparameterized queries / Consider Forced Parameterization'
        ELSE 'Plan cache composition is healthy'
    END AS "Recommendation"
FROM classified AS c
ORDER BY 1
"""

# Upstream counts a sample as pressure at indicator >= 2 (sp_pressuredetector's threshold),
# then splits medium (== 2) from severe (>= 3). Its bars stack medium onto severe within
# each source side by side; Grafana stacks one group, so all four share a stack.
_PRESSURE_EVENTS_SQL = f"""
SELECT
    date_trunc('hour', mpe.sample_time) AS time,
    srv.name || ' - ' || s.series AS metric,
    COUNT(*) AS value
FROM {collector('memory_pressure_events')} AS mpe
{server_join('mpe.server_id')}
CROSS JOIN LATERAL (VALUES
    ('SQL Server (medium)', mpe.memory_indicators_process = 2),
    ('SQL Server (severe)', mpe.memory_indicators_process >= 3),
    ('Operating System (medium)', mpe.memory_indicators_system = 2),
    ('Operating System (severe)', mpe.memory_indicators_system >= 3)
) AS s(series, hit)
WHERE $__timeFilter(mpe.sample_time)
  AND {server_filter('mpe.server_id')}
  AND (mpe.memory_indicators_process >= 2 OR mpe.memory_indicators_system >= 2)
  AND s.hit
GROUP BY 1, 2
ORDER BY 1
"""

_MB_COLUMNS = (
    "Physical Memory",
    "SQL Server Memory",
    "Target Memory",
    "Buffer Pool",
    "Plan Cache",
    "Available Physical",
    "Total Page File",
    "Available Page File",
)


def memory():
    """Build the Memory dashboard."""
    reset_id()
    panels: list[dict] = []

    y = subtab(
        panels,
        "Overview",
        0,
        [
            (
                24,
                6,
                lambda x, y, w, h: table(
                    "Memory Summary",
                    x,
                    y,
                    w,
                    h,
                    _SUMMARY_SQL,
                    overrides=[col_unit(c, "decmbytes") for c in _MB_COLUMNS],
                    description="Latest collected memory state, independent of the time range.",
                ),
            ),
            (
                24,
                10,
                lambda x, y, w, h: timeseries(
                    "Memory Trend",
                    x,
                    y,
                    w,
                    h,
                    [target(_MEMORY_TREND_SQL)],
                    unit="decgbytes",
                    axis_label="Memory (GB)",
                ),
            ),
        ],
    )

    y = subtab(
        panels,
        "Memory Clerks",
        y,
        [
            (
                24,
                10,
                lambda x, y, w, h: timeseries(
                    "Memory Clerks",
                    x,
                    y,
                    w,
                    h,
                    [target(_CLERK_TREND_SQL)],
                    unit="decmbytes",
                    axis_label="Memory (MB)",
                ),
            ),
            (
                24,
                5,
                lambda x, y, w, h: table(
                    "Clerk Summary",
                    x,
                    y,
                    w,
                    h,
                    _CLERK_SUMMARY_SQL,
                    overrides=[
                        col_unit("Non-BP Total (MB)", "decmbytes"),
                        col_unit("Top Non-BP Clerk (MB)", "decmbytes"),
                    ],
                ),
            ),
        ],
    )

    y = subtab(
        panels,
        "Memory Grants",
        y,
        [
            (
                24,
                9,
                lambda x, y, w, h: timeseries(
                    "Memory Grant Sizing",
                    x,
                    y,
                    w,
                    h,
                    [target(_GRANT_SIZING_SQL)],
                    unit="decmbytes",
                    axis_label="Memory (MB)",
                ),
            ),
            (
                24,
                9,
                lambda x, y, w, h: timeseries(
                    "Memory Grant Activity",
                    x,
                    y,
                    w,
                    h,
                    [target(_GRANT_ACTIVITY_SQL)],
                    axis_label="Count",
                ),
            ),
        ],
    )

    y = subtab(
        panels,
        "Plan Cache",
        y,
        [
            (
                24,
                9,
                lambda x, y, w, h: timeseries(
                    "Plan Cache Size",
                    x,
                    y,
                    w,
                    h,
                    [target(_PLAN_CACHE_TREND_SQL)],
                    unit="decmbytes",
                    axis_label="Plan Cache Size (MB)",
                ),
            ),
            (
                24,
                5,
                lambda x, y, w, h: table(
                    "Plan Cache Summary",
                    x,
                    y,
                    w,
                    h,
                    _PLAN_CACHE_SUMMARY_SQL,
                ),
            ),
        ],
    )

    subtab(
        panels,
        "Memory Pressure Events",
        y,
        [
            (
                24,
                9,
                lambda x, y, w, h: timeseries(
                    "Memory Pressure Events",
                    x,
                    y,
                    w,
                    h,
                    [target(_PRESSURE_EVENTS_SQL)],
                    bars=True,
                    stacked=True,
                    axis_label="Pressure Events per Hour",
                ),
            ),
        ],
    )

    clerk_var = query_var(
        "clerk_type",
        "Memory clerk",
        _CLERK_TYPES_QUERY,
        "Memory clerks collected over the window, heaviest first. All plots the top 5.",
    )

    return dashboard(uid("memory"), "Memory", panels, [server_var(), clerk_var])
