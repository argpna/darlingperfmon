"""tempdb dashboard (Darling line).

Upstream ref: ViewerDataService.TempDb.cs, RenderTempDb*Chart (ViewerServerTab.Charts.cs).
"""

from ._shared import (
    collector,
    dashboard,
    flow,
    reset_id,
    server_filter,
    server_join,
    server_var,
    target,
    timeseries,
    uid,
)

_USAGE_SQL = f"""
SELECT
    td.collection_time AS time,
    srv.name || ' - ' || s.series AS metric,
    s.value AS value
FROM {collector('tempdb_stats')} AS td
{server_join('td.server_id')}
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
_SIZE_SQL = f"""
SELECT
    td.collection_time AS time,
    srv.name AS metric,
    td.total_reserved_mb + td.unallocated_mb AS value
FROM {collector('tempdb_stats')} AS td
{server_join('td.server_id')}
WHERE $__timeFilter(td.collection_time)
  AND {server_filter('td.server_id')}
ORDER BY 1
"""

_FILE_IO_SQL = f"""
SELECT
    f.collection_time AS time,
    srv.name || ' - ' || f.file_name || s.suffix AS metric,
    s.value AS value
FROM {collector('file_io_stats')} AS f
{server_join('f.server_id')}
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


def tempdb():
    """Build the tempdb dashboard."""
    reset_id()
    panels: list[dict] = []

    flow(
        panels,
        0,
        [
            (
                24,
                9,
                lambda x, y, w, h: timeseries(
                    "tempdb Usage",
                    x,
                    y,
                    w,
                    h,
                    [target(_USAGE_SQL)],
                    unit="decmbytes",
                    axis_label="MB",
                ),
            ),
            (
                24,
                7,
                lambda x, y, w, h: timeseries(
                    "tempdb Allocated Size",
                    x,
                    y,
                    w,
                    h,
                    [target(_SIZE_SQL)],
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
                    [target(_FILE_IO_SQL)],
                    unit="ms",
                    axis_label="tempdb File I/O Latency (ms)",
                ),
            ),
        ],
    )

    return dashboard(uid("tempdb"), "tempdb", panels, [server_var()])
