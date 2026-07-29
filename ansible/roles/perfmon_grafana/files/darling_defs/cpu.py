"""CPU dashboard (Darling line).

Upstream ref: ViewerDataService.Cpu.cs, ViewerDataService.CpuScheduler.cs,
CpuSchedulerMetrics.BuildMetrics (PerformanceMonitor.Common).
"""

from ._shared import (
    collector,
    dashboard,
    reset_id,
    server_filter,
    server_join,
    server_var,
    status_colors,
    subtab,
    table,
    target,
    timeseries,
    uid,
)

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
    srv.name || ' - ' || series AS metric,
    value
FROM {collector('cpu_utilization_stats')} AS cpu
{server_join('cpu.server_id')}
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
    srv.name || ' - ' || series AS metric,
    value
FROM {collector('cpu_scheduler_stats')} AS cs
{server_join('cs.server_id')}
CROSS JOIN LATERAL (VALUES
    ('Runnable Tasks', cs.total_runnable_tasks_count),
    ('Blocked Tasks', cs.total_blocked_task_count),
    ('Queued Requests', cs.total_queued_request_count)
) AS s(series, value)
WHERE $__timeFilter(cs.collection_time)
  AND {server_filter('cs.server_id')}
ORDER BY 1
"""


def _n0(expr: str) -> str:
    """C# N0: thousands-separated integer."""
    return f"to_char({expr}, 'FM999,999,999,990')"


def _fixed(expr: str, places: int) -> str:
    """C# F1/F2: fixed decimal places, no thousands separator."""
    mask = "FM999999990" + ("." + "0" * places if places else "")
    return f"to_char(round(({expr})::numeric, {places}), '{mask}')"


def _kb_as_gb(expr: str) -> str:
    """C# FormatKbAsGb: GB to one decimal at 1 GB and above, otherwise whole MB."""
    return f"""CASE
            WHEN {expr} / 1048576.0 >= 1
            THEN {_fixed(f'{expr} / 1048576.0', 1)} || ' GB'
            ELSE {_fixed(f'{expr} / 1024.0', 0)} || ' MB'
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
        ("Schedulers", _n0("l.scheduler_count"), "false"),
        ("Logical CPUs", _n0("l.cpu_count"), "false"),
        (
            "NUMA Nodes (online / total)",
            "l.nodes_online_count || ' / ' || l.total_node_count",
            "false",
        ),
        ("Offline CPUs", _n0("l.offline_cpu_count"), "l.offline_cpu_warning"),
        ("Max Worker Threads", _n0("l.max_workers_count"), "false"),
        (
            "Current Workers",
            _n0("l.total_current_workers_count"),
            "l.worker_thread_exhaustion_warning",
        ),
        (
            "Worker Utilization %",
            _fixed("l.worker_util", 1),
            "l.worker_thread_exhaustion_warning",
        ),
        (
            "Runnable Tasks",
            _n0("l.total_runnable_tasks_count"),
            "l.runnable_tasks_warning",
        ),
        (
            "Avg Runnable / Scheduler",
            _fixed("l.avg_runnable_tasks_count", 2),
            "l.runnable_tasks_warning",
        ),
        ("Work Queue Length", _n0("l.total_work_queue_count"), "false"),
        ("Active Requests", _n0("l.total_active_request_count"), "false"),
        (
            "Queued Requests",
            _n0("l.total_queued_request_count"),
            "l.queued_requests_warning",
        ),
        ("Blocked Tasks", _n0("l.total_blocked_task_count"), "l.blocked_tasks_warning"),
        (
            "Active Parallel Threads",
            _n0("l.total_active_parallel_thread_count"),
            "false",
        ),
        (
            "Runnable Requests",
            _na("l.runnable_request_count", _n0("l.runnable_request_count")),
            "false",
        ),
        (
            "Total Requests",
            _na("l.total_request_count", _n0("l.total_request_count")),
            "false",
        ),
        (
            "Runnable %",
            _na("l.runnable_percent", _fixed("l.runnable_percent", 2)),
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


def cpu():
    """Build the CPU dashboard."""
    reset_id()
    panels: list[dict] = []

    y = subtab(
        panels,
        "CPU Utilization",
        0,
        [
            (
                24,
                10,
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
        ],
    )

    subtab(
        panels,
        "CPU Scheduler",
        y,
        [
            (
                24,
                10,
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

    return dashboard(uid("cpu"), "CPU", panels, [server_var()])
