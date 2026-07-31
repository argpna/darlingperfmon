"""FinOps Capacity & Growth dashboard (Darling line) - merges Database Sizes and
Storage Growth dashboards: current size snapshot and growth trend.

Upstream ref: ViewerDataService.FinOps.Storage.cs.

Preserves the Storage Growth -> Object Sizes & Growth -> Index Detail drill-down chain;
only the uid the link is defined inside (this dashboard's) has changed.
"""

from .._shared import (
    UTC_NOW,
    col_datalink,
    col_gauge_bar,
    col_hidden,
    col_thresholds,
    col_unit,
    collector,
    finops_dashboard,
    n0,
    reset_id,
    server_filter,
    server_join,
    server_var,
    subtab,
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

_SIZES_SQL = f"""
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


def _growth_snapshot_cte(name: str, extra: str = "") -> str:
    """Per-database allocated size at one comparison point.

    Upstream picks `MAX(collection_time) <= cutoff` for its single server; per server here,
    or the server collected most recently would set everyone else's baseline.
    """
    return f"""{name} AS (
    SELECT server_id, database_name, SUM(total_size_mb) AS size_mb
    FROM {_SIZES}
    WHERE {server_filter()} AND {latest_per_server(_SIZES, extra)}
    GROUP BY server_id, database_name
)"""


_GROWTH_SQL = f"""
WITH {_growth_snapshot_cte('latest')},
{_growth_snapshot_cte('past_7d', f" AND collection_time <= {UTC_NOW} - INTERVAL '7 days'")},
{_growth_snapshot_cte('past_30d', f" AND collection_time <= {UTC_NOW} - INTERVAL '30 days'")}
SELECT
    l.server_id AS "server_id",
    srv.name AS "Server",
    l.database_name AS "Database",
    round(l.size_mb, 2) AS "Current Size MB",
    round(p7.size_mb, 2) AS "Size 7d Ago MB",
    round(p30.size_mb, 2) AS "Size 30d Ago MB",
    round(l.size_mb - COALESCE(p7.size_mb, l.size_mb), 2) AS "Growth 7d MB",
    round(l.size_mb - COALESCE(p30.size_mb, l.size_mb), 2) AS "Growth 30d MB",
    round(CASE
        WHEN p30.size_mb IS NOT NULL THEN (l.size_mb - p30.size_mb) / 30.0
        WHEN p7.size_mb IS NOT NULL THEN (l.size_mb - p7.size_mb) / 7.0
        ELSE 0
    END, 2) AS "Daily Rate MB",
    round(CASE
        WHEN p30.size_mb IS NOT NULL AND p30.size_mb > 0
        THEN (l.size_mb - p30.size_mb) * 100.0 / p30.size_mb
        ELSE 0
    END, 1) AS "Growth % 30d"
FROM latest l
LEFT JOIN past_7d p7 ON p7.server_id = l.server_id AND p7.database_name = l.database_name
LEFT JOIN past_30d p30
       ON p30.server_id = l.server_id AND p30.database_name = l.database_name
{server_join('l.server_id')}
ORDER BY l.size_mb - COALESCE(p30.size_mb, l.size_mb) DESC
"""


def capacity_growth():
    """Build the FinOps Capacity & Growth dashboard."""
    reset_id()
    panels: list[dict] = []

    y = subtab(
        panels,
        "Database Sizes",
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
                    _SIZES_SQL,
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

    # Upstream's "Show objects" context-menu item / grid double-click.
    drill = col_datalink(
        "Database",
        "Show objects",
        "/d/darling-finops-object-sizes?${__url_time_range}"
        "&var-server=${__data.fields.server_id}"
        "&var-database=${__data.fields.Database}",
    )

    subtab(
        panels,
        "Storage Growth",
        y,
        [
            (
                24,
                20,
                lambda x, y, w, h: table(
                    "Storage Growth",
                    x,
                    y,
                    w,
                    h,
                    _GROWTH_SQL,
                    overrides=[
                        col_hidden("server_id"),
                        drill,
                        col_unit("Current Size MB", "mbytes"),
                        col_unit("Size 7d Ago MB", "mbytes"),
                        col_unit("Size 30d Ago MB", "mbytes"),
                        col_unit("Growth 7d MB", "mbytes"),
                        col_unit("Growth 30d MB", "mbytes"),
                        col_unit("Daily Rate MB", "mbytes"),
                        col_thresholds(
                            "Growth % 30d", ("text", None), ("yellow", 10), ("red", 25)
                        ),
                    ],
                    sort_by=[{"displayName": "Growth 30d MB", "desc": True}],
                    description=(
                        "Allocated size now versus 7 and 30 days ago; a database with no "
                        "snapshot that far back shows no growth. Click one to drill in."
                    ),
                ),
            )
        ],
    )

    return finops_dashboard(
        uid("finops-capacity-growth"),
        "Capacity & Growth",
        panels,
        [server_var()],
        time_from="now-30d",
        refresh="15m",
    )
