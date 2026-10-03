"""FinOps Capacity & Growth dashboard - merges Database Sizes and
Storage Growth dashboards: current size snapshot and growth trend, plus the
persistent version store.

Upstream ref: ViewerDataService.FinOps.Storage.cs, ViewerDataService.FinOps.Pvs.cs.

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
    timeseries,
    target,
    uid,
)
from ._shared import budget_cte, latest_per_server

_SIZES = collector("database_size_stats")
_PVS = collector("pvs_stats")

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


_PVS_SQL = f"""
WITH p AS (
    SELECT *
    FROM {_PVS}
    WHERE {server_filter()} AND {latest_per_server(_PVS)}
)
SELECT
    srv.name AS "Server",
    p.database_name AS "Database",
    CASE p.is_accelerated_database_recovery_on
        WHEN TRUE THEN 'On' WHEN FALSE THEN 'Off' ELSE '-'
    END AS "ADR",
    round(p.persistent_version_store_size_mb, 2) AS "PVS Off-Row MB",
    round(p.persistent_version_store_size_mb * 100.0
          / NULLIF(p.database_data_size_mb, 0), 1) AS "% of DB",
    round(p.database_data_size_mb, 2) AS "Data Files MB",
    round(p.online_index_version_store_size_mb, 2) AS "Online Index MB",
    p.current_aborted_transaction_count AS "Aborted Txns",
    p.oldest_active_transaction_id AS "Oldest Active Txn",
    p.oldest_aborted_transaction_id AS "Oldest Aborted Txn",
    CASE WHEN p.oldest_aborted_transaction_id > 0 AND p.oldest_active_transaction_id > 0
         THEN p.oldest_active_transaction_id - p.oldest_aborted_transaction_id
    END AS "Aborted Lag",
    CASE
        WHEN (p.aborted_version_cleaner_start_time IS NOT NULL
              AND p.aborted_version_cleaner_end_time IS NULL)
          OR (p.offrow_version_cleaner_start_time IS NOT NULL
              AND p.offrow_version_cleaner_end_time IS NULL) THEN 'Running'
        WHEN p.aborted_version_cleaner_end_time IS NOT NULL
          OR p.offrow_version_cleaner_end_time IS NOT NULL THEN 'Idle'
        ELSE 'Never run'
    END AS "Cleanup",
    GREATEST(p.aborted_version_cleaner_end_time,
             p.offrow_version_cleaner_end_time) AS "Last Cleanup End",
    p.pvs_off_row_page_skipped_low_water_mark AS "Skipped: Secondary",
    p.pvs_off_row_page_skipped_min_useful_xts AS "Skipped: Snapshot",
    p.pvs_off_row_page_skipped_oldest_aborted_xdesid AS "Skipped: Aborted"
FROM p
{server_join('p.server_id')}
ORDER BY p.persistent_version_store_size_mb DESC NULLS LAST, p.database_name
"""


def _pvs_trend_sql(value: str) -> str:
    return f"""
WITH latest AS (
    SELECT server_id, database_name,
           row_number() OVER (
               PARTITION BY server_id
               ORDER BY persistent_version_store_size_mb DESC NULLS LAST, database_name
           ) AS rn
    FROM {_PVS}
    WHERE {server_filter()} AND {latest_per_server(_PVS)}
)
SELECT
    p.collection_time AS time,
    srv.name || ' / ' || p.database_name AS metric,
    {value} AS value
FROM {_PVS} p
JOIN latest l
  ON l.server_id = p.server_id AND l.database_name = p.database_name AND l.rn <= 5
{server_join('p.server_id')}
WHERE $__timeFilter(p.collection_time)
  AND p.persistent_version_store_size_mb IS NOT NULL
ORDER BY 1
"""


_PVS_SIZE_SQL = _pvs_trend_sql("p.persistent_version_store_size_mb::double precision")
_PVS_PCT_SQL = _pvs_trend_sql(
    "p.persistent_version_store_size_mb * 100.0 / NULLIF(p.database_data_size_mb, 0)"
)


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

    y = subtab(
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

    subtab(
        panels,
        "Version Store (PVS)",
        y,
        [
            (
                12,
                9,
                lambda x, y, w, h: timeseries(
                    "PVS Off-Row Size",
                    x,
                    y,
                    w,
                    h,
                    [target(_PVS_SIZE_SQL)],
                    unit="mbytes",
                    axis_label="PVS Off-Row (MB)",
                    description="Top databases by current PVS size, per server.",
                ),
            ),
            (
                12,
                9,
                lambda x, y, w, h: timeseries(
                    "PVS % of Database",
                    x,
                    y,
                    w,
                    h,
                    [target(_PVS_PCT_SQL)],
                    unit="percent",
                    axis_label="PVS % of Data Files",
                    description="Top databases by current PVS size, per server.",
                ),
            ),
            (
                24,
                14,
                lambda x, y, w, h: table(
                    "Accelerated Database Recovery - Persistent Version Store",
                    x,
                    y,
                    w,
                    h,
                    _PVS_SQL,
                    overrides=[
                        col_unit("PVS Off-Row MB", "mbytes"),
                        col_unit("Data Files MB", "mbytes"),
                        col_unit("Online Index MB", "mbytes"),
                        col_thresholds(
                            "% of DB", ("text", None), ("yellow", 25), ("red", 50)
                        ),
                    ],
                    sort_by=[{"displayName": "PVS Off-Row MB", "desc": True}],
                    description=(
                        "PVS sizes count off-row versions only. When a large PVS does "
                        "not shrink, the skipped-page counters will reflect it: "
                        "Secondary means a query on a secondary replica, Snapshot means "
                        "a long-running snapshot scan, Aborted means space still held "
                        "by aborted transactions. SQL Server 2019 or later."
                    ),
                ),
            ),
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
