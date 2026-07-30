"""FinOps Locking & Contention dashboard (Darling line).

Upstream ref: IndexLockingAllSql / IndexLockingByDbSql / IndexLockingDatabasesSql
(ViewerDataService.FinOps.Locking.cs), FinOpsTab.Locking.cs.

Upstream's per-column log shading becomes a gauge cell and its database combo becomes
$database. Its double-click detail pane is omitted: every column it shows is already in the
grid. Counters are cumulative since restart, so these are lifetime totals, not rates.
"""

from .._shared import (
    col_gauge_bar,
    col_unit,
    collector,
    finops_dashboard,
    flow,
    multi_filter,
    query_var,
    reset_id,
    server_filter,
    server_join,
    server_var,
    table,
    uid,
)

_IOS = collector("index_object_stats")

# IndexLockingAllSql's top-N.
_TOP_N = 200

# Shared with the $database variable's query, so the selector lists only what the grid shows.
_HAS_CONTENTION = """(
    COALESCE(ios.row_lock_wait_in_ms, 0) > 0
    OR COALESCE(ios.page_lock_wait_in_ms, 0) > 0
    OR COALESCE(ios.page_latch_wait_in_ms, 0) > 0
    OR COALESCE(ios.page_io_latch_wait_in_ms, 0) > 0
    OR COALESCE(ios.index_lock_promotion_count, 0) > 0
)"""

_TOTAL_WAIT = """COALESCE(ios.row_lock_wait_in_ms, 0)
    + COALESCE(ios.page_lock_wait_in_ms, 0)
    + COALESCE(ios.page_latch_wait_in_ms, 0)
    + COALESCE(ios.page_io_latch_wait_in_ms, 0)"""

# Per-database latest: collection cycles differ per database.
_LATEST_PER_DB = f"""latest AS (
    SELECT server_id, database_name, MAX(collection_time) AS latest_time
    FROM {_IOS}
    WHERE {server_filter()}
    GROUP BY server_id, database_name
)"""

_DB_VAR_SQL = f"""
WITH {_LATEST_PER_DB}
SELECT DISTINCT ios.database_name
FROM {_IOS} AS ios
JOIN latest l ON l.server_id = ios.server_id
             AND l.database_name = ios.database_name
             AND l.latest_time = ios.collection_time
WHERE {server_filter('ios.server_id')} AND {_HAS_CONTENTION}
ORDER BY ios.database_name
"""

_SQL = f"""
WITH {_LATEST_PER_DB},
contended AS (
    SELECT ios.*,
           {_TOTAL_WAIT} AS total_wait_ms,
           ROW_NUMBER() OVER (
               PARTITION BY ios.server_id ORDER BY {_TOTAL_WAIT} DESC) AS rn
    FROM {_IOS} AS ios
    JOIN latest l ON l.server_id = ios.server_id
                 AND l.database_name = ios.database_name
                 AND l.latest_time = ios.collection_time
    WHERE {server_filter('ios.server_id')}
      AND {_HAS_CONTENTION}
      AND {multi_filter('ios.database_name', 'database')}
)
SELECT
    srv.name AS "Server",
    c.database_name AS "Database",
    c.schema_name AS "Schema",
    c.table_name AS "Table",
    COALESCE(c.index_name, '(heap)') AS "Index",
    c.index_type_desc AS "Type",
    COALESCE(c.row_lock_count, 0) AS "Row Locks",
    COALESCE(c.row_lock_wait_count, 0) AS "Row Lock Waits",
    COALESCE(c.row_lock_wait_in_ms, 0) AS "Row Lock Wait ms",
    COALESCE(c.page_lock_count, 0) AS "Page Locks",
    COALESCE(c.page_lock_wait_count, 0) AS "Page Lock Waits",
    COALESCE(c.page_lock_wait_in_ms, 0) AS "Page Lock Wait ms",
    COALESCE(c.index_lock_promotion_count, 0) AS "Lock Escalations",
    COALESCE(c.page_latch_wait_count, 0) AS "Page Latch Waits",
    COALESCE(c.page_latch_wait_in_ms, 0) AS "Page Latch Wait ms",
    COALESCE(c.page_io_latch_wait_count, 0) AS "Page IO Latch Waits",
    COALESCE(c.page_io_latch_wait_in_ms, 0) AS "Page IO Latch Wait ms",
    round(c.reserved_mb, 1) AS "Reserved MB",
    c.total_rows AS "Rows"
FROM contended c
{server_join('c.server_id')}
WHERE c.rn <= {_TOP_N}
ORDER BY c.total_wait_ms DESC
"""


def locking():
    """Build the FinOps Locking & Contention dashboard."""
    reset_id()
    panels: list[dict] = []

    # No max, so each bar scales to its own column - upstream's per-column intensity.
    wait_gauges = [
        col_gauge_bar(col, unit="ms", max_val=None)
        for col in (
            "Row Lock Wait ms",
            "Page Lock Wait ms",
            "Page Latch Wait ms",
            "Page IO Latch Wait ms",
        )
    ]

    flow(
        panels,
        0,
        [
            (
                24,
                20,
                lambda x, y, w, h: table(
                    "Locking & Contention",
                    x,
                    y,
                    w,
                    h,
                    _SQL,
                    overrides=wait_gauges + [col_unit("Reserved MB", "mbytes")],
                    sort_by=[{"displayName": "Row Lock Wait ms", "desc": True}],
                    description=(
                        f"Top {_TOP_N} indexes per server by total lock + latch wait, at "
                        "each database's latest snapshot. Lifetime totals, not rates."
                    ),
                ),
            )
        ],
    )

    return finops_dashboard(
        uid("finops-locking"),
        "FinOps - Locking & Contention",
        panels,
        [
            server_var(),
            query_var(
                "database",
                "Database",
                _DB_VAR_SQL,
                "Databases with recorded lock or latch contention.",
            ),
        ],
    )
