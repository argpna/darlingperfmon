"""Perfmon dashboard (Darling line).

Upstream ref: ViewerDataService.Perfmon.cs, ViewerServerTab.Perfmon.cs,
PerfmonPacks (PerformanceMonitor.Ui).
"""

from ._shared import (
    collector,
    custom_var,
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

# perfmon_stats is cumulative since restart; upstream charts delta_cntr_value, not cntr_value.
# Counters are summed across their instance_name rows, matching upstream's GROUP BY, and the
# series count is capped at upstream's 12.
_TREND_SQL = f"""
WITH ranked AS (
    SELECT
        pm.server_id,
        srv.name AS server_label,
        pm.counter_name
    FROM {collector('perfmon_stats')} AS pm
    {server_join('pm.server_id')}
    WHERE $__timeFilter(pm.collection_time)
      AND {server_filter('pm.server_id')}
      AND {_PACK_FILTER}
      AND {multi_filter('pm.counter_name', 'counter')}
    GROUP BY pm.server_id, srv.name, pm.counter_name
    ORDER BY SUM(pm.delta_cntr_value) DESC
    LIMIT 12
)
SELECT
    pm.collection_time AS time,
    r.server_label || ' - ' || pm.counter_name AS metric,
    SUM(pm.delta_cntr_value) AS value
FROM {collector('perfmon_stats')} AS pm
JOIN ranked AS r
  ON r.server_id = pm.server_id
 AND r.counter_name = pm.counter_name
WHERE $__timeFilter(pm.collection_time)
  AND {server_filter('pm.server_id')}
GROUP BY pm.collection_time, r.server_label, pm.counter_name
ORDER BY 1
"""


def perfmon():
    """Build the Perfmon dashboard."""
    reset_id()
    panels: list[dict] = []

    flow(
        panels,
        0,
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
                    [target(_TREND_SQL)],
                    axis_label="Value",
                ),
            ),
        ],
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
        uid("perfmon"), "Perfmon", panels, [server_var(), pack_var, counter_var]
    )
