"""Storage & tempdb dashboard - merges File I/O and tempdb
dashboards: disk-facing resource pressure, split out from CPU/Memory & Sessions because it
answers a different question ("is storage the bottleneck" vs "is compute the bottleneck").

Upstream ref: ViewerDataService.FileIo.cs, ViewerServerTab.FileIo.cs,
ViewerDataService.TempDb.cs, RenderTempDb*Chart (ViewerServerTab.Charts.cs).

$server is single-select, so every series label below is just the file/metric identifier -
no server prefix needed to disambiguate.
"""

from ._shared import (
    collector,
    dashboard,
    fixed,
    flow,
    reset_id,
    server_filter,
    server_join,
    server_var,
    stat_grid,
    subtab,
    target,
    thresholds,
    timeseries,
    uid,
    UTC_NOW,
)

# Stat row: total data file size, average read/write latency, tempdb allocated %,
# worst-latency file. Short trailing windows, not $__timeFilter - "right now" snapshot
# tiles, matching Overview's and Collection Health's stat rows.
_TOTAL_DATA_FILE_SIZE_SQL = f"""
WITH latest AS (
    SELECT DISTINCT ON (database_name, file_name)
        database_name, file_name, file_type_desc, total_size_mb
    FROM {collector('database_size_stats')}
    WHERE {server_filter()}
    ORDER BY database_name, file_name, collection_time DESC
)
SELECT COALESCE(SUM(total_size_mb), 0) AS v
FROM latest
WHERE upper(file_type_desc) <> 'LOG'
"""

_AVG_RW_LATENCY_SQL = f"""
SELECT CASE WHEN SUM(delta_reads + delta_writes) > 0
    THEN SUM(delta_stall_read_ms + delta_stall_write_ms)::double precision
         / SUM(delta_reads + delta_writes)
    ELSE 0 END AS v
FROM {collector('file_io_stats')}
WHERE collection_time >= {UTC_NOW} - INTERVAL '1 hour' AND {server_filter()}
"""

_TEMPDB_ALLOCATED_PCT_SQL = f"""
SELECT CASE WHEN (total_reserved_mb + unallocated_mb) > 0
    THEN total_reserved_mb::double precision / (total_reserved_mb + unallocated_mb) * 100.0
    ELSE 0 END AS v
FROM {collector('tempdb_stats')}
WHERE {server_filter()}
ORDER BY collection_time DESC
LIMIT 1
"""

_WORST_LATENCY_FILE_SQL = f"""
SELECT database_name || '.' || file_name || ' - ' || {fixed('avg_latency_ms', 1)} || ' ms' AS v
FROM (
    SELECT database_name, file_name,
        CASE WHEN SUM(delta_reads + delta_writes) > 0
            THEN SUM(delta_stall_read_ms + delta_stall_write_ms)::double precision
                 / SUM(delta_reads + delta_writes)
            ELSE 0 END AS avg_latency_ms
    FROM {collector('file_io_stats')}
    WHERE collection_time >= {UTC_NOW} - INTERVAL '1 hour' AND {server_filter()}
    GROUP BY database_name, file_name
) AS f
ORDER BY avg_latency_ms DESC
LIMIT 1
"""

_STAT_ROW = [
    {
        "title": "Total Data File Size",
        "sql": _TOTAL_DATA_FILE_SIZE_SQL,
        "unit": "decmbytes",
        "th": thresholds(("text", None)),
    },
    {
        "title": "Avg Read/Write Latency",
        "sql": _AVG_RW_LATENCY_SQL,
        "unit": "ms",
        "th": thresholds(("text", None)),
    },
    {
        "title": "tempdb Allocated %",
        "sql": _TEMPDB_ALLOCATED_PCT_SQL,
        "unit": "percent",
        "th": thresholds(("text", None)),
    },
    {
        "title": "Worst-Latency File",
        "sql": _WORST_LATENCY_FILE_SQL,
        "th": thresholds(("text", None)),
        "fields": "/.*/",
    },
]


# File I/O section.
# Both sub-tabs chart the ten busiest files, ranked on the metric each one is about:
# latency by I/O count, throughput by bytes moved.
def _top_files(rank_expr: str, activity: str) -> str:
    return f"""
    SELECT f.server_id, f.database_name, f.file_name
    FROM {collector('file_io_stats')} AS f
    WHERE $__timeFilter(f.collection_time)
      AND {server_filter('f.server_id')}
      AND ({activity})
    GROUP BY f.server_id, f.database_name, f.file_name
    ORDER BY SUM({rank_expr}) DESC
    LIMIT 10
"""


def _latency_sql(direction: str) -> str:
    """Upstream splits read and write onto their own chart, queued latency alongside."""
    count = "f.delta_reads" if direction == "read" else "f.delta_writes"
    stall = "f.delta_stall_read_ms" if direction == "read" else "f.delta_stall_write_ms"
    queued = (
        "f.delta_stall_queued_read_ms"
        if direction == "read"
        else "f.delta_stall_queued_write_ms"
    )
    return f"""
WITH top_files AS (
{_top_files('f.delta_reads + f.delta_writes', 'f.delta_reads > 0 OR f.delta_writes > 0')}
)
SELECT
    f.collection_time AS time,
    f.database_name || '.' || f.file_name || s.suffix AS metric,
    s.value AS value
FROM {collector('file_io_stats')} AS f
JOIN top_files AS tf
  ON tf.server_id = f.server_id
 AND tf.database_name = f.database_name
 AND tf.file_name = f.file_name
CROSS JOIN LATERAL (VALUES
    ('', CASE WHEN {count} > 0
              THEN {stall}::double precision / {count}
              ELSE 0 END),
    (' (queued)', CASE WHEN {count} > 0
                       THEN COALESCE({queued}, 0)::double precision / {count}
                       ELSE 0 END)
) AS s(suffix, value)
WHERE $__timeFilter(f.collection_time)
  AND {server_filter('f.server_id')}
ORDER BY 1
"""


def _throughput_sql(column: str) -> str:
    """MB/s per file, from the per-interval bytes divided by the elapsed seconds."""
    return f"""
WITH top_files AS (
{_top_files('f.delta_read_bytes + f.delta_write_bytes',
            'f.delta_read_bytes > 0 OR f.delta_write_bytes > 0')}
),
windowed AS (
    SELECT
        f.collection_time,
        f.database_name || '.' || f.file_name AS file_label,
        f.{column} AS moved_bytes,
        EXTRACT(EPOCH FROM (f.collection_time - LAG(f.collection_time) OVER (
            PARTITION BY f.server_id, f.database_name, f.file_name
            ORDER BY f.collection_time))) AS interval_seconds
    FROM {collector('file_io_stats')} AS f
    JOIN top_files AS tf
      ON tf.server_id = f.server_id
     AND tf.database_name = f.database_name
     AND tf.file_name = f.file_name
    WHERE $__timeFilter(f.collection_time)
      AND {server_filter('f.server_id')}
)
SELECT
    collection_time AS time,
    file_label AS metric,
    moved_bytes::double precision / interval_seconds / 1048576.0 AS value
FROM windowed
WHERE interval_seconds > 0
ORDER BY 1
"""


# tempdb section.
_TEMPDB_USAGE_SQL = f"""
SELECT
    td.collection_time AS time,
    s.series AS metric,
    s.value AS value
FROM {collector('tempdb_stats')} AS td
CROSS JOIN LATERAL (VALUES
    ('User Objects', td.user_object_reserved_mb),
    ('Internal Objects', td.internal_object_reserved_mb),
    ('Version Store', td.version_store_reserved_mb)
) AS s(series, value)
WHERE $__timeFilter(td.collection_time)
  AND {server_filter('td.server_id')}
ORDER BY 1
"""

# Upstream charts total size on its own axis so growth does not flatten the usage series.
_TEMPDB_SIZE_SQL = f"""
SELECT
    td.collection_time AS time,
    'Allocated Size' AS metric,
    td.total_reserved_mb + td.unallocated_mb AS value
FROM {collector('tempdb_stats')} AS td
WHERE $__timeFilter(td.collection_time)
  AND {server_filter('td.server_id')}
ORDER BY 1
"""

_TEMPDB_FILE_IO_SQL = f"""
SELECT
    f.collection_time AS time,
    f.file_name || s.suffix AS metric,
    s.value AS value
FROM {collector('file_io_stats')} AS f
CROSS JOIN LATERAL (VALUES
    (' read', CASE WHEN f.delta_reads > 0
                   THEN f.delta_stall_read_ms::double precision / f.delta_reads
                   ELSE 0 END),
    (' write', CASE WHEN f.delta_writes > 0
                    THEN f.delta_stall_write_ms::double precision / f.delta_writes
                    ELSE 0 END)
) AS s(suffix, value)
WHERE $__timeFilter(f.collection_time)
  AND {server_filter('f.server_id')}
  AND f.database_name = 'tempdb'
ORDER BY 1
"""


def storage_tempdb():
    """Build the Storage & tempdb dashboard."""
    reset_id()
    panels: list[dict] = []

    y = flow(panels, 0, [(24, 4, stat_grid(_STAT_ROW, cols=4))])

    y = subtab(
        panels,
        "File I/O",
        y,
        [
            (
                12,
                9,
                lambda x, y, w, h: timeseries(
                    "Read Latency",
                    x,
                    y,
                    w,
                    h,
                    [target(_latency_sql("read"))],
                    unit="ms",
                    axis_label="Read Latency (ms)",
                ),
            ),
            (
                12,
                9,
                lambda x, y, w, h: timeseries(
                    "Write Latency",
                    x,
                    y,
                    w,
                    h,
                    [target(_latency_sql("write"))],
                    unit="ms",
                    axis_label="Write Latency (ms)",
                ),
            ),
            (
                12,
                9,
                lambda x, y, w, h: timeseries(
                    "Read Throughput",
                    x,
                    y,
                    w,
                    h,
                    [target(_throughput_sql("delta_read_bytes"))],
                    unit="MBs",
                    axis_label="Read Throughput (MB/s)",
                ),
            ),
            (
                12,
                9,
                lambda x, y, w, h: timeseries(
                    "Write Throughput",
                    x,
                    y,
                    w,
                    h,
                    [target(_throughput_sql("delta_write_bytes"))],
                    unit="MBs",
                    axis_label="Write Throughput (MB/s)",
                ),
            ),
        ],
    )

    subtab(
        panels,
        "tempdb",
        y,
        [
            (
                12,
                9,
                lambda x, y, w, h: timeseries(
                    "tempdb Usage",
                    x,
                    y,
                    w,
                    h,
                    [target(_TEMPDB_USAGE_SQL)],
                    unit="decmbytes",
                    axis_label="MB",
                ),
            ),
            (
                12,
                9,
                lambda x, y, w, h: timeseries(
                    "tempdb Allocated Size",
                    x,
                    y,
                    w,
                    h,
                    [target(_TEMPDB_SIZE_SQL)],
                    unit="decmbytes",
                    axis_label="Allocated MB",
                ),
            ),
            (
                24,
                9,
                lambda x, y, w, h: timeseries(
                    "tempdb File I/O Latency",
                    x,
                    y,
                    w,
                    h,
                    [target(_TEMPDB_FILE_IO_SQL)],
                    unit="ms",
                    axis_label="tempdb File I/O Latency (ms)",
                ),
            ),
        ],
    )

    return dashboard(uid("storage-tempdb"), "Storage & tempdb", panels, [server_var()])
