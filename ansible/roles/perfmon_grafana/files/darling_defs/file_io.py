"""File I/O dashboard (Darling line).

Upstream ref: ViewerDataService.FileIo.cs, ViewerServerTab.FileIo.cs.
"""

from ._shared import (
    collector,
    dashboard,
    reset_id,
    server_filter,
    server_join,
    server_var,
    subtab,
    target,
    timeseries,
    uid,
)


# Both sub-tabs chart the ten busiest files, ranked on the metric each one is about:
# latency by I/O count, throughput by bytes moved.
def _top_files(rank_expr: str, activity: str) -> str:
    return f"""
    SELECT
        f.server_id,
        srv.name AS server_label,
        f.database_name,
        f.file_name
    FROM {collector('file_io_stats')} AS f
    {server_join('f.server_id')}
    WHERE $__timeFilter(f.collection_time)
      AND {server_filter('f.server_id')}
      AND ({activity})
    GROUP BY f.server_id, srv.name, f.database_name, f.file_name
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
    tf.server_label || ' - ' || f.database_name || '.' || f.file_name || s.suffix AS metric,
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
        tf.server_label,
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
    server_label || ' - ' || file_label AS metric,
    moved_bytes::double precision / interval_seconds / 1048576.0 AS value
FROM windowed
WHERE interval_seconds > 0
ORDER BY 1
"""


def file_io():
    """Build the File I/O dashboard."""
    reset_id()
    panels: list[dict] = []

    y = subtab(
        panels,
        "File I/O Latency",
        0,
        [
            (
                24,
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
                24,
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
        ],
    )

    subtab(
        panels,
        "File I/O Throughput",
        y,
        [
            (
                24,
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
                24,
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

    return dashboard(uid("file-io"), "File I/O", panels, [server_var()])
