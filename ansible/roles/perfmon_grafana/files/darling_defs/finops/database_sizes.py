"""FinOps Database Sizes dashboard (Darling line).

Upstream ref: DatabaseSizeLatestSql (ViewerDataService.FinOps.Storage.cs), DatabaseSizeRow's
computed columns (ViewerDataService.FinOps.cs), LoadFinOpsDatabaseSizesAsync's cost share.

Biggest files first, interleaved across databases: this is a capacity view, not a browse.
"""

from .._shared import (
    col_gauge_bar,
    col_unit,
    collector,
    finops_dashboard,
    flow,
    n0,
    reset_id,
    server_filter,
    server_join,
    server_var,
    table,
    uid,
)
from ._shared import budget_cte, latest_per_server

_SIZES = collector("database_size_stats")

# GrowthDisplay. NULL is-percent-growth means the setting could not be read.
_GROWTH_DISPLAY = f"""CASE
        WHEN s.is_percent_growth IS NULL THEN '-'
        WHEN s.is_percent_growth THEN
            CASE WHEN s.growth_pct IS NULL THEN '-'
                 ELSE s.growth_pct || '%' END
        WHEN COALESCE(s.auto_growth_mb, 0) = 0 THEN 'Disabled'
        ELSE {n0('s.auto_growth_mb')} || ' MB'
    END"""

# VlfCountDisplay: VLFs exist only in the log, so a data file reads N/A rather than 0.
_VLF_DISPLAY = """CASE
        WHEN upper(s.file_type_desc) = 'LOG'
        THEN COALESCE(s.vlf_count::text, '-')
        ELSE 'N/A'
    END"""

_SQL = f"""
WITH latest AS (
    SELECT *
    FROM {_SIZES}
    WHERE {server_filter()} AND {latest_per_server(_SIZES)}
),
server_totals AS (
    /* Cost share is by size within the server, so the denominator is per server. */
    SELECT server_id, NULLIF(SUM(total_size_mb), 0) AS total_mb
    FROM latest
    GROUP BY server_id
),
{budget_cte()}
SELECT
    srv.name AS "Server",
    s.database_name AS "Database",
    s.file_type_desc AS "File Type",
    s.file_name AS "File Name",
    round(s.total_size_mb, 2) AS "Total Size MB",
    round(s.used_size_mb, 2) AS "Used Size MB",
    round(s.total_size_mb - s.used_size_mb, 2) AS "Free Space MB",
    round(s.used_size_mb * 100.0 / NULLIF(s.total_size_mb, 0), 1) AS "Used %",
    s.volume_mount_point AS "Volume",
    round(s.volume_total_mb, 0) AS "Volume Total MB",
    round(s.volume_free_mb, 0) AS "Volume Free MB",
    s.recovery_model_desc AS "Recovery Model",
    {_GROWTH_DISPLAY} AS "Auto Growth",
    {_VLF_DISPLAY} AS "VLF Count",
    round(COALESCE(s.total_size_mb / t.total_mb * b.monthly_cost, 0), 2)
        AS "Monthly Cost ($)"
FROM latest s
JOIN server_totals t ON t.server_id = s.server_id
LEFT JOIN budget b ON b.server_id = s.server_id
{server_join('s.server_id')}
ORDER BY s.total_size_mb DESC, s.database_name, s.file_type_desc, s.file_name
"""


def database_sizes():
    """Build the FinOps Database Sizes dashboard."""
    reset_id()
    panels: list[dict] = []

    flow(
        panels,
        0,
        [
            (
                24,
                20,
                lambda x, y, w, h: table(
                    "Database Sizes",
                    x,
                    y,
                    w,
                    h,
                    _SQL,
                    overrides=[
                        col_unit("Total Size MB", "mbytes"),
                        col_unit("Used Size MB", "mbytes"),
                        col_unit("Free Space MB", "mbytes"),
                        col_gauge_bar("Used %"),
                        col_unit("Volume Total MB", "mbytes"),
                        col_unit("Volume Free MB", "mbytes"),
                        col_unit("Monthly Cost ($)", "currencyUSD"),
                    ],
                    sort_by=[{"displayName": "Total Size MB", "desc": True}],
                    description=(
                        "Latest snapshot, one row per file. Monthly Cost is the file's "
                        "share of the server budget by size, 0 until one is configured."
                    ),
                ),
            )
        ],
    )

    return finops_dashboard(
        uid("finops-database-sizes"),
        "Database Sizes",
        panels,
        [server_var()],
    )
