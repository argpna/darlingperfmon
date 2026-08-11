"""FinOps object-growth drill-down.

Upstream ref: ObjectGrowthSummarySql / ObjectGrowthSeriesSql
(ViewerDataService.FinOps.Storage.cs), FinOpsTab.ObjectHeatmap.cs.

Reached from FinOps - Storage Growth. Upstream's ScottPlot object-by-day heatmap becomes one
time series per object. Its window combo becomes $window, not the dashboard range, because the
growth figures are measured against that window's own endpoints.
"""

from .._shared import (
    UTC_NOW,
    col_datalink,
    col_thresholds,
    col_unit,
    collector,
    custom_var,
    detail_dashboard,
    flow,
    reset_id,
    server_filter,
    server_var,
    table,
    target,
    text_var,
    timeseries,
    uid,
)

_IOS = collector("index_object_stats")

# FinOpsObjectHeatmapWindowCombo, defaulting to its SelectedIndex="1" (30 days).
_WINDOWS = ["30", "7", "90"]

_WINDOW_START = f"{UTC_NOW} - (${{window}} || ' days')::interval"

# GetObjectGrowthHeatmapDataAsync's topN.
_TOP_N = 20

# Sentinel-guarded: ${database:sqlstring} collapses to a zero-length token on a cold load.
_DB_FILTER = (
    "(${database:sqlstring} = '*' OR ${database:sqlstring} = ''"
    " OR ios.database_name = ${database:sqlstring})"
)

# Newest and oldest snapshot inside the window, not the window edges.
_BOUNDS = f"""bounds AS (
    SELECT MAX(collection_time) AS latest_time, MIN(collection_time) AS earliest_time
    FROM {_IOS} AS ios
    WHERE {server_filter('ios.server_id')}
      AND {_DB_FILTER}
      AND ios.collection_time >= {_WINDOW_START}
)"""

_RANKED = f"""
WITH {_BOUNDS},
latest AS (
    SELECT ios.schema_name, ios.table_name,
           SUM(ios.reserved_mb) AS cur_reserved_mb,
           SUM(ios.used_mb) AS cur_used_mb,
           MAX(ios.total_rows) AS cur_rows,
           COUNT(*) AS index_count
    FROM {_IOS} AS ios
    WHERE {server_filter('ios.server_id')}
      AND {_DB_FILTER}
      AND ios.collection_time = (SELECT latest_time FROM bounds)
    GROUP BY ios.schema_name, ios.table_name
),
earliest AS (
    SELECT ios.schema_name, ios.table_name, SUM(ios.reserved_mb) AS e_reserved_mb
    FROM {_IOS} AS ios
    WHERE {server_filter('ios.server_id')}
      AND {_DB_FILTER}
      AND ios.collection_time = (SELECT earliest_time FROM bounds)
    GROUP BY ios.schema_name, ios.table_name
),
ranked AS (
    SELECT l.*,
           l.cur_reserved_mb - COALESCE(e.e_reserved_mb, l.cur_reserved_mb) AS growth_mb
    FROM latest l
    LEFT JOIN earliest e
           ON e.schema_name = l.schema_name AND e.table_name = l.table_name
    ORDER BY growth_mb DESC, l.schema_name, l.table_name
    LIMIT {_TOP_N}
)"""

_SUMMARY_SQL = f"""
{_RANKED}
SELECT
    r.schema_name AS "Schema",
    r.table_name AS "Table",
    round(r.cur_reserved_mb, 1) AS "Reserved MB",
    round(r.cur_used_mb, 1) AS "Used MB",
    r.cur_rows AS "Rows",
    r.index_count AS "Indexes",
    round(r.growth_mb, 1) AS "Growth MB",
    round(CASE
        WHEN r.cur_reserved_mb - r.growth_mb > 0
        THEN r.growth_mb * 100.0 / (r.cur_reserved_mb - r.growth_mb)
        ELSE 0
    END, 1) AS "Growth %",
    round(r.growth_mb / NULLIF(${{window}}::numeric, 0), 2) AS "Daily Rate MB"
FROM ranked r
ORDER BY r.growth_mb DESC, r.schema_name, r.table_name
"""

_SERIES_SQL = f"""
{_RANKED}
SELECT
    date_trunc('day', ios.collection_time) AS time,
    ios.schema_name || '.' || ios.table_name AS metric,
    SUM(ios.reserved_mb) AS reserved_mb
FROM {_IOS} AS ios
JOIN ranked r ON r.schema_name = ios.schema_name AND r.table_name = ios.table_name
WHERE {server_filter('ios.server_id')}
  AND {_DB_FILTER}
  AND ios.collection_time >= {_WINDOW_START}
GROUP BY 1, 2
ORDER BY 1, 2
"""


def object_sizes():
    """Build the FinOps object-growth drill-down dashboard."""
    reset_id()
    panels: list[dict] = []

    # Upstream's object-grid double-click into per-index detail.
    drill = col_datalink(
        "Table",
        "Show indexes",
        "/d/darling-finops-index-usage?${__url_time_range}"
        "&var-server=${server}&var-database=${database}"
        "&var-schema=${__data.fields.Schema}&var-table=${__data.fields.Table}",
    )

    flow(
        panels,
        0,
        [
            (
                24,
                10,
                lambda x, y, w, h: timeseries(
                    "Object Reserved Footprint (daily)",
                    x,
                    y,
                    w,
                    h,
                    [target(_SERIES_SQL)],
                    unit="mbytes",
                    description=(
                        f"Top {_TOP_N} objects by growth over the selected window. This "
                        "is per-object reservation, not file allocation, so it may not "
                        "fully explain file-level growth."
                    ),
                ),
            ),
            (
                24,
                14,
                lambda x, y, w, h: table(
                    "Object Growth Detail",
                    x,
                    y,
                    w,
                    h,
                    _SUMMARY_SQL,
                    overrides=[
                        drill,
                        col_unit("Reserved MB", "mbytes"),
                        col_unit("Used MB", "mbytes"),
                        col_unit("Growth MB", "mbytes"),
                        col_unit("Daily Rate MB", "mbytes"),
                        col_thresholds(
                            "Growth %", ("text", None), ("yellow", 10), ("red", 25)
                        ),
                    ],
                    sort_by=[{"displayName": "Growth MB", "desc": True}],
                    description=(
                        "Index/object stats are collected daily, so a window shorter than "
                        "two collections shows no growth."
                    ),
                ),
            ),
        ],
    )

    return detail_dashboard(
        uid("finops-object-sizes"),
        "Object Sizes & Growth",
        panels,
        [
            server_var(),
            text_var("database", "Database", "*"),
            custom_var(
                "window",
                "Window (days)",
                _WINDOWS,
                "Object-growth lookback, upstream's 7/30/90-day combo.",
            ),
        ],
        time_from="now-30d",
        prefix="FinOps",
    )
