"""FinOps Storage Growth dashboard (Darling line).

Upstream ref: StorageGrowthSql (ViewerDataService.FinOps.Storage.cs).

Grafana has no in-panel navigation stack, so upstream's three-level double-click drill becomes
three dashboards linked by data links - object_sizes.py and index_usage.py are the lower two.
"""

from .._shared import (
    UTC_NOW,
    col_datalink,
    col_hidden,
    col_thresholds,
    col_unit,
    collector,
    finops_dashboard,
    flow,
    reset_id,
    server_filter,
    server_join,
    server_var,
    table,
    uid,
)
from ._shared import latest_per_server

_SIZES = collector("database_size_stats")


def _snapshot_cte(name: str, extra: str = "") -> str:
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


_SQL = f"""
WITH {_snapshot_cte('latest')},
{_snapshot_cte('past_7d', f" AND collection_time <= {UTC_NOW} - INTERVAL '7 days'")},
{_snapshot_cte('past_30d', f" AND collection_time <= {UTC_NOW} - INTERVAL '30 days'")}
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


def storage_growth():
    """Build the FinOps Storage Growth dashboard."""
    reset_id()
    panels: list[dict] = []

    # Upstream's "Show objects" context-menu item / grid double-click.
    drill = col_datalink(
        "Database",
        "Show objects",
        "/d/darling-finops-object-sizes?${__url_time_range}"
        "&var-server=${__data.fields.server_id}"
        "&var-database=${__data.fields.Database}",
    )

    flow(
        panels,
        0,
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
                    _SQL,
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
        uid("finops-storage-growth"),
        "FinOps - Storage Growth",
        panels,
        [server_var()],
        time_from="now-30d",
        refresh="15m",
    )
