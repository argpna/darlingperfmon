"""CPU, Memory & Sessions dashboard - merges CPU, Memory, Session
Stats, and Perfmon Counters dashboards into one engine-resource-pressure view.

Upstream ref: ViewerDataService.Cpu.cs, ViewerDataService.Memory.cs,
ViewerDataService.SessionStats.cs, ViewerDataService.Perfmon.cs.

$server is single-select, so series labels carry just the metric/class/counter name, not a
server prefix.
"""

from ._shared import (
    col_unit,
    collector,
    custom_var,
    dashboard,
    fixed,
    flow,
    multi_filter,
    n0,
    query_var,
    reset_id,
    server_filter,
    server_join,
    server_var,
    stat_grid,
    status_colors,
    subtab,
    table,
    target,
    thresholds,
    timeseries,
    uid,
)

# Stat row: CPU%, worker utilization%, runnable tasks, buffer pool MB, active sessions,
# memory pressure state - promoted from table rows CPU's scheduler snapshot and Memory's
# summary already compute, so a reader gets them without reading a whole table.
_CURRENT_CPU_SQL = f"""
SELECT sqlserver_cpu_utilization + COALESCE(other_process_cpu_utilization, 0) AS v
FROM {collector('cpu_utilization_stats')}
WHERE {server_filter()}
ORDER BY collection_time DESC
LIMIT 1
"""

# Upstream ref: CpuSchedulerMetrics - the same 90% cutoff _PRESSURE_LEVEL below uses for
# "HIGH - Worker thread exhaustion".
_WORKER_UTIL_SQL = f"""
SELECT CASE WHEN max_workers_count > 0
    THEN total_current_workers_count::double precision / max_workers_count * 100.0
    ELSE 0 END AS v
FROM {collector('cpu_scheduler_stats')}
WHERE {server_filter()}
ORDER BY collection_time DESC
LIMIT 1
"""

_RUNNABLE_TASKS_SQL = f"""
SELECT total_runnable_tasks_count AS v
FROM {collector('cpu_scheduler_stats')}
WHERE {server_filter()}
ORDER BY collection_time DESC
LIMIT 1
"""

_BUFFER_POOL_SQL = f"""
SELECT buffer_pool_mb AS v
FROM {collector('memory_stats')}
WHERE {server_filter()}
ORDER BY collection_time DESC
LIMIT 1
"""

_ACTIVE_SESSIONS_SQL = f"""
SELECT total_sessions AS v
FROM {collector('session_summary_stats')}
WHERE {server_filter()}
ORDER BY collection_time DESC
LIMIT 1
"""

_MEMORY_PRESSURE_STATE_SQL = f"""
SELECT COALESCE(NULLIF(system_memory_state, ''), 'N/A') AS v
FROM {collector('memory_stats')}
WHERE {server_filter()}
ORDER BY collection_time DESC
LIMIT 1
"""

_STAT_ROW = [
    {
        "title": "CPU %",
        "sql": _CURRENT_CPU_SQL,
        "unit": "percent",
        "th": thresholds(("green", None), ("yellow", 80), ("red", 95)),
    },
    {
        "title": "Worker Utilization %",
        "sql": _WORKER_UTIL_SQL,
        "unit": "percent",
        "th": thresholds(("green", None), ("red", 90)),
    },
    {
        # Upstream ref: CpuSchedulerMetrics.ClassifyCpuPressure's own 10/20 cutoffs.
        "title": "Runnable Tasks",
        "sql": _RUNNABLE_TASKS_SQL,
        "th": thresholds(("green", None), ("yellow", 11), ("red", 21)),
    },
    {
        "title": "Buffer Pool MB",
        "sql": _BUFFER_POOL_SQL,
        "unit": "decmbytes",
        "th": thresholds(("text", None)),
    },
    {
        "title": "Active Sessions",
        "sql": _ACTIVE_SESSIONS_SQL,
        "th": thresholds(("text", None)),
    },
    {
        "title": "Memory Pressure State",
        "sql": _MEMORY_PRESSURE_STATE_SQL,
        "th": thresholds(("text", None)),
        "fields": "/.*/",
    },
]

# CPU section.
# sample_time is the monitored server's LOCAL wall clock, unlike every other stored
# timestamp. Upstream recovers the offset from the collection batch: the newest sample in a
# batch is under a minute older than its collection_time, so rounding that difference to 15
# minutes yields the server's UTC offset. Window on collection_time, plot the corrected
# sample_time. Upstream ref: CpuUtilizationSql (#1262).
_SAMPLE_TIME_UTC = """cpu.sample_time
        - INTERVAL '15 minutes'
          * ROUND(EXTRACT(EPOCH FROM (
                MAX(cpu.sample_time) OVER (PARTITION BY cpu.server_id, cpu.collection_time)
                - cpu.collection_time
            )) / 900.0)::double precision"""

_CPU_UTILIZATION_SQL = f"""
SELECT
    {_SAMPLE_TIME_UTC} AS time,
    s.series AS metric,
    s.value
FROM {collector('cpu_utilization_stats')} AS cpu
CROSS JOIN LATERAL (VALUES
    ('SQL Server', cpu.sqlserver_cpu_utilization),
    ('Other', COALESCE(cpu.other_process_cpu_utilization, 0))
) AS s(series, value)
WHERE $__timeFilter(cpu.collection_time)
  AND {server_filter('cpu.server_id')}
ORDER BY 1
"""

# A point-in-time collector: one row per collection, so the counts plot as stored.
_SCHEDULER_TREND_SQL = f"""
SELECT
    cs.collection_time AS time,
    s.series AS metric,
    s.value
FROM {collector('cpu_scheduler_stats')} AS cs
CROSS JOIN LATERAL (VALUES
    ('Runnable Tasks', cs.total_runnable_tasks_count),
    ('Blocked Tasks', cs.total_blocked_task_count),
    ('Queued Requests', cs.total_queued_request_count)
) AS s(series, value)
WHERE $__timeFilter(cs.collection_time)
  AND {server_filter('cs.server_id')}
ORDER BY 1
"""


def _kb_as_gb(expr: str) -> str:
    """C# FormatKbAsGb: GB to one decimal at 1 GB and above, otherwise whole MB."""
    return f"""CASE
            WHEN {expr} / 1048576.0 >= 1
            THEN {fixed(f'{expr} / 1048576.0', 1)} || ' GB'
            ELSE {fixed(f'{expr} / 1024.0', 0)} || ' MB'
        END"""


def _na(expr: str, formatted: str) -> str:
    """C# FormatNullableInt / the nullable percent row: 'N/A' when the column is null."""
    return f"CASE WHEN {expr} IS NULL THEN 'N/A' ELSE {formatted} END"


# Upstream ref: CpuSchedulerMetrics.ClassifyCpuPressure - banding order is significant.
_PRESSURE_LEVEL = """CASE
        WHEN l.total_runnable_tasks_count > 50 THEN 'CRITICAL - High runnable task queue'
        WHEN l.total_runnable_tasks_count > 20 THEN 'HIGH - Moderate runnable task queue'
        WHEN l.total_runnable_tasks_count > 10 THEN 'MEDIUM - Some runnable tasks queued'
        WHEN l.worker_util > 90 THEN 'HIGH - Worker thread exhaustion'
        WHEN l.worker_thread_exhaustion_warning THEN 'CRITICAL - Worker thread exhaustion warning'
        WHEN l.runnable_tasks_warning THEN 'HIGH - Runnable tasks warning'
        WHEN l.queued_requests_warning THEN 'MEDIUM - Queued requests warning'
        ELSE 'NORMAL'
    END"""

_RECOMMENDATION = """CASE
        WHEN l.total_runnable_tasks_count > 20
        THEN 'CPU pressure detected - check for CPU-intensive queries, consider adding CPU cores'
        WHEN l.worker_thread_exhaustion_warning
        THEN 'Worker thread exhaustion - check max worker threads setting'
        WHEN l.total_queued_request_count > 0
        THEN 'Requests queued for execution - CPU or worker thread pressure'
        ELSE 'No CPU scheduler pressure detected'
    END"""


def _scheduler_snapshot_sql() -> str:
    """The latest snapshot as a metric/value grid, in upstream's row order."""
    rows = [
        ("Pressure Level", "l.pressure_level", "l.pressure_level NOT LIKE 'NORMAL%'"),
        ("Recommendation", "l.recommendation", "false"),
        ("Schedulers", n0("l.scheduler_count"), "false"),
        ("Logical CPUs", n0("l.cpu_count"), "false"),
        (
            "NUMA Nodes (online / total)",
            "l.nodes_online_count || ' / ' || l.total_node_count",
            "false",
        ),
        ("Offline CPUs", n0("l.offline_cpu_count"), "l.offline_cpu_warning"),
        ("Max Worker Threads", n0("l.max_workers_count"), "false"),
        (
            "Current Workers",
            n0("l.total_current_workers_count"),
            "l.worker_thread_exhaustion_warning",
        ),
        (
            "Worker Utilization %",
            fixed("l.worker_util", 1),
            "l.worker_thread_exhaustion_warning",
        ),
        (
            "Runnable Tasks",
            n0("l.total_runnable_tasks_count"),
            "l.runnable_tasks_warning",
        ),
        (
            "Avg Runnable / Scheduler",
            fixed("l.avg_runnable_tasks_count", 2),
            "l.runnable_tasks_warning",
        ),
        ("Work Queue Length", n0("l.total_work_queue_count"), "false"),
        ("Active Requests", n0("l.total_active_request_count"), "false"),
        (
            "Queued Requests",
            n0("l.total_queued_request_count"),
            "l.queued_requests_warning",
        ),
        ("Blocked Tasks", n0("l.total_blocked_task_count"), "l.blocked_tasks_warning"),
        (
            "Active Parallel Threads",
            n0("l.total_active_parallel_thread_count"),
            "false",
        ),
        (
            "Runnable Requests",
            _na("l.runnable_request_count", n0("l.runnable_request_count")),
            "false",
        ),
        (
            "Total Requests",
            _na("l.total_request_count", n0("l.total_request_count")),
            "false",
        ),
        (
            "Runnable %",
            _na("l.runnable_percent", fixed("l.runnable_percent", 2)),
            "false",
        ),
        ("Total Physical Memory", _kb_as_gb("l.total_physical_memory_kb"), "false"),
        (
            "Available Physical Memory",
            _kb_as_gb("l.available_physical_memory_kb"),
            "l.physical_memory_pressure_warning",
        ),
        (
            "System Memory State",
            "COALESCE(NULLIF(l.system_memory_state_desc, ''), 'N/A')",
            "l.physical_memory_pressure_warning",
        ),
        (
            "Physical Memory Pressure",
            "CASE WHEN l.physical_memory_pressure_warning THEN 'Yes' ELSE 'No' END",
            "l.physical_memory_pressure_warning",
        ),
    ]
    values = ",\n    ".join(
        f"({i}, '{label}', {value}, {warn})"
        for i, (label, value, warn) in enumerate(rows)
    )
    return f"""
WITH snapshot AS (
    SELECT DISTINCT ON (cs.server_id)
        cs.*,
        srv.name AS server_label,
        CASE
            WHEN cs.max_workers_count > 0
            THEN cs.total_current_workers_count::double precision
                 / cs.max_workers_count * 100.0
            ELSE 0
        END AS worker_util
    FROM {collector('cpu_scheduler_stats')} AS cs
    {server_join('cs.server_id')}
    WHERE $__timeFilter(cs.collection_time)
      AND {server_filter('cs.server_id')}
    ORDER BY cs.server_id, cs.collection_time DESC
),
latest AS (
    SELECT
        l.*,
        {_PRESSURE_LEVEL} AS pressure_level,
        {_RECOMMENDATION} AS recommendation
    FROM snapshot AS l
)
SELECT
    l.server_label AS "Server",
    m.metric AS "Metric",
    m.value AS "Value",
    CASE WHEN m.warn THEN 'Warning' ELSE 'OK' END AS "Status"
FROM latest AS l
CROSS JOIN LATERAL (VALUES
    {values}
) AS m(ord, metric, value, warn)
ORDER BY l.server_label, m.ord
"""


# Memory Overview section.
# Upstream's summary strip reads the newest row with no window at all - it is current
# state, not a view of the selected range.
_MEMORY_SUMMARY_SQL = f"""
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
    s.series AS metric,
    s.value / 1024.0 AS value
FROM {collector('memory_stats')} AS ms
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
    'Memory Grants' AS metric,
    SUM(mg.granted_memory_mb) / 1024.0 AS value
FROM {collector('memory_grant_stats')} AS mg
WHERE $__timeFilter(mg.collection_time)
  AND {server_filter('mg.server_id')}
GROUP BY mg.collection_time
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

# Memory Clerks section.
_CLERK_TYPES_QUERY = f"""
SELECT mc.clerk_type
FROM {collector('memory_clerks')} AS mc
WHERE $__timeFilter(mc.collection_time)
  AND {server_filter('mc.server_id')}
GROUP BY mc.clerk_type
ORDER BY SUM(mc.memory_mb) DESC
"""

# Upstream's picker pre-selects the five heaviest clerks; All means the same five here.
# server_label survives here (unlike the trend below) because _CLERK_SUMMARY_SQL's "Server"
# table column still needs it.
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
    mc.clerk_type AS metric,
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


# Memory Grants section.
def _grant_sql(metrics: str) -> str:
    """Per-pool grant series, one metric per LATERAL row as upstream charts them."""
    return f"""
SELECT
    mg.collection_time AS time,
    'Pool ' || mg.pool_id || ': ' || s.series AS metric,
    SUM(s.value) AS value
FROM {collector('memory_grant_stats')} AS mg
CROSS JOIN LATERAL (VALUES
    {metrics}
) AS s(series, value)
WHERE $__timeFilter(mg.collection_time)
  AND {server_filter('mg.server_id')}
GROUP BY mg.collection_time, mg.pool_id, s.series
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

# Plan Cache section.
_PLAN_CACHE_TREND_SQL = f"""
SELECT
    pc.collection_time AS time,
    s.series AS metric,
    SUM(s.value) AS value
FROM {collector('plan_cache_stats')} AS pc
CROSS JOIN LATERAL (VALUES
    ('Single-Use', pc.single_use_size_mb),
    ('Multi-Use', pc.multi_use_size_mb)
) AS s(series, value)
WHERE $__timeFilter(pc.collection_time)
  AND {server_filter('pc.server_id')}
GROUP BY pc.collection_time, s.series
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

# Memory Pressure Events section.
# Upstream counts a sample as pressure at indicator >= 2 (sp_pressuredetector's threshold),
# then splits medium (== 2) from severe (>= 3). Its bars stack medium onto severe within
# each source side by side; Grafana stacks one group, so all four share a stack.
_PRESSURE_EVENTS_SQL = f"""
SELECT
    date_trunc('hour', mpe.sample_time) AS time,
    s.series AS metric,
    COUNT(*) AS value
FROM {collector('memory_pressure_events')} AS mpe
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

# Session Stats section.
# Upstream skips a status that is zero across the whole window so all-zero states stay out
# of the legend; the HAVING-equivalent WHERE below does the same here.
_SESSION_COUNTS_SQL = f"""
WITH expanded AS (
    SELECT
        ss.collection_time,
        s.series,
        s.value,
        MAX(s.value) OVER (PARTITION BY s.series) AS series_max
    FROM {collector('session_summary_stats')} AS ss
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
    series AS metric,
    value
FROM expanded
WHERE series_max > 0
ORDER BY 1
"""

# The strip under upstream's chart: attribution from the newest snapshot in the window, the
# values a time series cannot carry.
_SESSION_ATTRIBUTION_SQL = f"""
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

# Perfmon Counters section.
ALL_COUNTERS = "All Counters"

# Upstream ref: PerfmonPacks.Packs - the same named counter groups Lite and the Dashboard use.
PACKS = {
    "General Throughput": [
        "Batch Requests/sec",
        "SQL Compilations/sec",
        "SQL Re-Compilations/sec",
        "Query optimizations/sec",
        "Network IO waits",
    ],
    "Memory Pressure": [
        "Memory Grants Pending",
        "Granted Workspace Memory (KB)",
        "Target Server Memory (KB)",
        "Total Server Memory (KB)",
        "Stolen Server Memory (KB)",
        "Lock Memory (KB)",
        "SQL Cache Memory (KB)",
        "Lazy writes/sec",
        "Free list stalls/sec",
        "Reduced memory grants/sec",
        "Memory grant queue waits",
        "Thread-safe memory objects waits",
        "Page reads/sec",
        "Readahead pages/sec",
    ],
    "CPU / Compilation": [
        "Batch Requests/sec",
        "SQL Compilations/sec",
        "SQL Re-Compilations/sec",
        "Query optimizations/sec",
        "Active parallel threads",
        "Active requests",
        "Queued requests",
        "Wait for the worker",
    ],
    "I/O Pressure": [
        "Page reads/sec",
        "Page writes/sec",
        "Checkpoint pages/sec",
        "Page lookups/sec",
        "Readahead pages/sec",
        "Background writer pages/sec",
        "Log Flushes/sec",
        "Log Bytes Flushed/sec",
        "Log Flush Write Time (ms)",
        "Page IO latch waits",
        "Log buffer waits",
        "Log write waits",
        "Full Scans/sec",
        "Index Searches/sec",
        "Page Splits/sec",
    ],
    "TempDB Pressure": [
        "Version Store Size (KB)",
        "Free Space in tempdb (KB)",
        "Active Temp Tables",
        "Version Generation rate (KB/s)",
        "Version Cleanup rate (KB/s)",
        "Temp Tables Creation Rate",
        "Workfiles Created/sec",
        "Worktables Created/sec",
    ],
    "Lock / Blocking": [
        "Lock Requests/sec",
        "Lock Wait Time (ms)",
        "Lock Waits/sec",
        "Number of Deadlocks/sec",
        "Table Lock Escalations/sec",
        "Blocked tasks",
        "Lock waits",
        "Non-Page latch waits",
        "Page latch waits",
        "Processes blocked",
        "Lock Timeouts/sec",
    ],
}

# Upstream defaults the pack combo to General Throughput, so it leads the list here.
PACK_NAMES = ["General Throughput"] + [
    name for name in PACKS if name != "General Throughput"
]

_PACK_MEMBERS = ",\n    ".join(
    f"('{pack}', '{counter}')"
    for pack, counters in PACKS.items()
    for counter in counters
)

_PACK_FILTER = f"""(${{pack:sqlstring}} = '{ALL_COUNTERS}'
       OR pm.counter_name IN (
              SELECT p.counter
              FROM (VALUES
    {_PACK_MEMBERS}
              ) AS p(pack, counter)
              WHERE p.pack = ${{pack:sqlstring}}
          ))"""

_COUNTERS_QUERY = f"""
SELECT DISTINCT pm.counter_name
FROM {collector('perfmon_stats')} AS pm
WHERE $__timeFilter(pm.collection_time)
  AND {server_filter('pm.server_id')}
  AND {_PACK_FILTER}
ORDER BY pm.counter_name
"""

# perfmon_stats is cumulative since restart; upstream charts delta_cntr_value, not
# cntr_value. Counters are summed across their instance_name rows, matching upstream's
# GROUP BY, and the series count is capped at upstream's 12.
_PERFMON_TREND_SQL = f"""
WITH ranked AS (
    SELECT pm.server_id, pm.counter_name
    FROM {collector('perfmon_stats')} AS pm
    WHERE $__timeFilter(pm.collection_time)
      AND {server_filter('pm.server_id')}
      AND {_PACK_FILTER}
      AND {multi_filter('pm.counter_name', 'counter')}
    GROUP BY pm.server_id, pm.counter_name
    ORDER BY SUM(pm.delta_cntr_value) DESC
    LIMIT 12
)
SELECT
    pm.collection_time AS time,
    pm.counter_name AS metric,
    SUM(pm.delta_cntr_value) AS value
FROM {collector('perfmon_stats')} AS pm
JOIN ranked AS r
  ON r.server_id = pm.server_id
 AND r.counter_name = pm.counter_name
WHERE $__timeFilter(pm.collection_time)
  AND {server_filter('pm.server_id')}
GROUP BY pm.collection_time, pm.counter_name
ORDER BY 1
"""


def cpu_memory_sessions():
    """Build the CPU, Memory & Sessions dashboard."""
    reset_id()
    panels: list[dict] = []

    y = flow(panels, 0, [(24, 4, stat_grid(_STAT_ROW, cols=6))])

    y = subtab(
        panels,
        "CPU",
        y,
        [
            (
                12,
                9,
                lambda x, y, w, h: timeseries(
                    "CPU Utilization",
                    x,
                    y,
                    w,
                    h,
                    [target(_CPU_UTILIZATION_SQL)],
                    unit="percent",
                    max_=105,
                    axis_label="CPU %",
                ),
            ),
            (
                12,
                9,
                lambda x, y, w, h: timeseries(
                    "Scheduler Pressure",
                    x,
                    y,
                    w,
                    h,
                    [target(_SCHEDULER_TREND_SQL)],
                    axis_label="Task Count",
                ),
            ),
            (
                24,
                12,
                lambda x, y, w, h: table(
                    "Latest Snapshot",
                    x,
                    y,
                    w,
                    h,
                    _scheduler_snapshot_sql(),
                    overrides=[
                        status_colors("Status", {"Warning": "red", "OK": "green"})
                    ],
                ),
            ),
        ],
    )

    y = subtab(
        panels,
        "Memory Overview",
        y,
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
                    _MEMORY_SUMMARY_SQL,
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
                12,
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
                12,
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
                12,
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
                12,
                9,
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

    y = subtab(
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

    y = subtab(
        panels,
        "Session Stats",
        y,
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
                    [target(_SESSION_COUNTS_SQL)],
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
                    _SESSION_ATTRIBUTION_SQL,
                ),
            ),
        ],
    )

    subtab(
        panels,
        "Perfmon Counters",
        y,
        [
            (
                24,
                14,
                lambda x, y, w, h: timeseries(
                    "Perfmon Counters",
                    x,
                    y,
                    w,
                    h,
                    [target(_PERFMON_TREND_SQL)],
                    axis_label="Value",
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
    pack_var = custom_var(
        "pack",
        "Counter pack",
        PACK_NAMES + [ALL_COUNTERS],
        "Named counter group, narrowing what the counter list offers.",
    )
    counter_var = query_var(
        "counter",
        "Counter",
        _COUNTERS_QUERY,
        "Counters in the selected pack that were collected over the window. "
        "All plots the 12 busiest.",
    )

    return dashboard(
        uid("cpu-memory-sessions"),
        "CPU, Memory & Sessions",
        panels,
        [server_var(), clerk_var, pack_var, counter_var],
    )
