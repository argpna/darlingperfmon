"""FinOps per-index detail drill-down (Darling line).

Upstream ref: ObjectIndexDetailSql (ViewerDataService.FinOps.Storage.cs).

Reached from FinOps - Object Sizes & Growth, which pre-fills the filters. Last Access is the
monitored server's local wall clock, not naive UTC like the rest of the line, so it is read
verbatim.
"""

from .._shared import (
    col_unit,
    collector,
    detail_dashboard,
    flow,
    reset_id,
    server_filter,
    server_var,
    status_colors,
    table,
    text_var,
    uid,
)
from ._shared import latest_per_server

_IOS = collector("index_object_stats")

CLASSIFICATION_COLORS = {"Unused": "red", "Write-only": "yellow", "Active": "green"}

# Sentinel-guarded: ${var:sqlstring} collapses to a zero-length token on a cold load.
_FILTERS = "\n  AND ".join(
    f"(${{{var}:sqlstring}} = '*' OR ${{{var}:sqlstring}} = ''"
    f" OR ios.{col} = ${{{var}:sqlstring}})"
    for var, col in (
        ("database", "database_name"),
        ("schema", "schema_name"),
        ("table", "table_name"),
    )
)

_READS = (
    "COALESCE(ios.user_seeks, 0) + COALESCE(ios.user_scans, 0)"
    " + COALESCE(ios.user_lookups, 0)"
)

_SQL = f"""
SELECT
    ios.database_name AS "Database",
    ios.schema_name AS "Schema",
    ios.table_name AS "Table",
    COALESCE(ios.index_name, '(heap)') AS "Index",
    CASE
        WHEN {_READS} = 0 AND COALESCE(ios.user_updates, 0) = 0 THEN 'Unused'
        WHEN {_READS} = 0 AND COALESCE(ios.user_updates, 0) > 0 THEN 'Write-only'
        ELSE 'Active'
    END AS "Status",
    ios.index_type_desc AS "Type",
    ios.index_id AS "Index ID",
    round(ios.reserved_mb, 1) AS "Reserved MB",
    ios.total_rows AS "Rows",
    COALESCE(ios.user_seeks, 0) AS "Seeks",
    COALESCE(ios.user_scans, 0) AS "Scans",
    COALESCE(ios.user_lookups, 0) AS "Lookups",
    {_READS} AS "Total Reads",
    COALESCE(ios.user_updates, 0) AS "Updates",
    GREATEST(ios.last_user_seek, ios.last_user_scan,
             ios.last_user_lookup, ios.last_user_update) AS "Last Access"
FROM {_IOS} AS ios
WHERE {server_filter('ios.server_id')}
  AND {latest_per_server(_IOS)}
  AND {_FILTERS}
ORDER BY ios.database_name, ios.schema_name, ios.table_name, ios.index_id
"""


def index_usage():
    """Build the FinOps per-index detail dashboard."""
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
                    "Index Detail",
                    x,
                    y,
                    w,
                    h,
                    _SQL,
                    overrides=[
                        status_colors("Status", CLASSIFICATION_COLORS),
                        col_unit("Reserved MB", "mbytes"),
                    ],
                    description=(
                        "Latest snapshot per server. Usage counters are lifetime totals "
                        "since an unreliable reset point - never diff them."
                    ),
                ),
            )
        ],
    )

    return detail_dashboard(
        uid("finops-index-usage"),
        "Index Detail",
        panels,
        [
            server_var(),
            text_var("database", "Database", "*"),
            text_var("schema", "Schema", "*"),
            text_var("table", "Table", "*"),
        ],
        prefix="FinOps",
    )
