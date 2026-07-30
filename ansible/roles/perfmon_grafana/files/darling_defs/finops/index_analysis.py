"""FinOps Index Analysis dashboard (Darling line).

Upstream ref: IndexObjectStatsLatestSql / ServerCompressionInfoSql /
DeriveIndexCleanupOptions (ViewerDataService.FinOps.IndexAnalysis.cs), IndexCleanupAnalyzer.cs.

Ports the analyzer's snapshot-derived half: the analyzable set, the Unused Index rule with both
uptime gates, compression candidacy, and the plain aggregates. Its consolidation rules and
generated DDL are not ported - see the dashboard's own text panel for what that leaves out.
"""

from .._shared import (
    col_thresholds,
    col_unit,
    collector,
    finops_dashboard,
    n0,
    reset_id,
    server_filter,
    server_join,
    server_var,
    status_colors,
    subtab,
    table,
    text_panel,
    uid,
)

_IOS = collector("index_object_stats")
_PROPS = collector("server_properties")

# DeriveIndexCleanupOptions' @can_compress. An unknown edition assumes compression is
# available, as upstream does.
_SUPPORTS_COMPRESSION = """CASE
        WHEN sp.engine_edition IS NULL THEN true
        WHEN sp.engine_edition IN (3, 5, 8) THEN true
        WHEN sp.engine_edition IN (2, 4)
             THEN COALESCE(split_part(sp.product_version, '.', 1)::int, 0) >= 13
        ELSE false
    END"""

# Uptime gates the unused verdict: caveat under 14 days, suppressed entirely at 7 or less.
# sqlserver_start_time is server-local, compared against a local clock upstream too.
_UPTIME_DAYS = (
    """EXTRACT(EPOCH FROM (LOCALTIMESTAMP - sp.sqlserver_start_time)) / 86400.0"""
)

_ANALYZABLE = """NOT COALESCE(ios.is_disabled, false)
      AND COALESCE(ios.index_name, '') <> ''
      AND upper(ios.index_type_desc) IN ('CLUSTERED', 'NONCLUSTERED')"""

_READS = (
    "COALESCE(ios.user_seeks, 0) + COALESCE(ios.user_scans, 0)"
    " + COALESCE(ios.user_lookups, 0)"
)

# Rule 1 (ApplyUnused), verbatim.
_IS_UNUSED = f"""({_READS} = 0
        AND upper(ios.index_type_desc) = 'NONCLUSTERED'
        AND NOT COALESCE(ios.is_primary_key, false)
        AND NOT COALESCE(ios.is_unique_constraint, false)
        AND NOT COALESCE(ios.is_unique, false)
        AND NOT COALESCE(ios.is_foreign_key_reference, false)
        AND ios.index_id <> 1)"""

_CAN_COMPRESS = f"""({_SUPPORTS_COMPRESSION}
        AND ios.index_id > 0
        AND upper(COALESCE(ios.data_compression_desc, '')) = 'NONE')"""

# Latest row per index identity, not per server-wide MAX: collection cycles differ per database.
_SNAPSHOT = f"""snapshot AS (
    SELECT DISTINCT ON (ios.server_id, ios.database_id, ios.object_id, ios.index_id)
           ios.*,
           {_SUPPORTS_COMPRESSION} AS supports_compression,
           {_UPTIME_DAYS} AS uptime_days,
           {_READS} AS reads,
           {_IS_UNUSED} AS is_unused_candidate,
           {_CAN_COMPRESS} AS can_compress
    FROM {_IOS} AS ios
    LEFT JOIN (
        SELECT DISTINCT ON (server_id) server_id, engine_edition, product_version,
               sqlserver_start_time
        FROM {_PROPS}
        ORDER BY server_id, collection_time DESC
    ) sp ON sp.server_id = ios.server_id
    WHERE {server_filter('ios.server_id')}
      AND {_ANALYZABLE}
    ORDER BY ios.server_id, ios.database_id, ios.object_id, ios.index_id,
             ios.collection_time DESC
),
analyzed AS (
    SELECT s.*,
           /* Dedupe-only at <= 7 days uptime suppresses Rule 1. */
           s.is_unused_candidate
               AND COALESCE(s.uptime_days, 999) > 7 AS is_unused
    FROM snapshot s
)"""

# BuildRollup's row count: the base row (index_id 0/1) per object, summed across objects.
_ROLLUP_SQL = f"""
WITH {_SNAPSHOT},
base_rows AS (
    SELECT DISTINCT ON (server_id, database_id, object_id)
           server_id, database_name, total_rows
    FROM analyzed
    WHERE index_id <= 1
    ORDER BY server_id, database_id, object_id, index_id
),
per_db AS (
    SELECT a.server_id,
           a.database_name,
           COUNT(DISTINCT a.object_id) AS tables_analyzed,
           COUNT(*) AS index_count,
           SUM(COALESCE(a.reserved_mb, 0)) / 1024.0 AS total_size_gb,
           COUNT(*) FILTER (WHERE a.is_unused) AS unused_indexes,
           COALESCE(SUM(COALESCE(a.reserved_mb, 0)) FILTER (WHERE a.is_unused), 0)
               / 1024.0 AS unused_size_gb,
           COUNT(*) FILTER (WHERE a.can_compress) AS compressable_indexes,
           SUM(COALESCE(a.user_seeks, 0)) AS user_seeks,
           SUM(COALESCE(a.user_scans, 0)) AS user_scans,
           SUM(COALESCE(a.user_lookups, 0)) AS user_lookups,
           SUM(COALESCE(a.user_updates, 0)) AS total_writes,
           SUM(COALESCE(a.row_lock_wait_count, 0)
               + COALESCE(a.page_lock_wait_count, 0)) AS lock_wait_count,
           SUM(COALESCE(a.row_lock_wait_in_ms, 0)
               + COALESCE(a.page_lock_wait_in_ms, 0)) AS lock_wait_ms,
           SUM(COALESCE(a.page_latch_wait_count, 0)
               + COALESCE(a.page_io_latch_wait_count, 0)) AS latch_wait_count,
           SUM(COALESCE(a.page_latch_wait_in_ms, 0)
               + COALESCE(a.page_io_latch_wait_in_ms, 0)) AS latch_wait_ms,
           MIN(a.uptime_days) AS uptime_days
    FROM analyzed a
    GROUP BY a.server_id, a.database_name
),
db_rows AS (
    SELECT server_id, database_name, SUM(COALESCE(total_rows, 0)) AS total_rows
    FROM base_rows
    GROUP BY server_id, database_name
),
scoped AS (
    /* ProjectRollups' order: one ALL DATABASES total per server, then a row per database.
       GROUPING() marks the total, which renders sp_IndexCleanup's SUMMARY-level 'N/A'. */
    SELECT p.server_id,
           GROUPING(p.database_name) AS is_overall,
           CASE WHEN GROUPING(p.database_name) = 1
                THEN 'ALL DATABASES' ELSE p.database_name END AS scope,
           SUM(p.tables_analyzed) AS tables_analyzed,
           SUM(p.index_count) AS index_count,
           SUM(p.total_size_gb) AS total_size_gb,
           SUM(p.unused_indexes) AS unused_indexes,
           SUM(p.unused_size_gb) AS unused_size_gb,
           SUM(p.compressable_indexes) AS compressable_indexes,
           SUM(p.user_seeks) AS user_seeks,
           SUM(p.user_scans) AS user_scans,
           SUM(p.user_lookups) AS user_lookups,
           SUM(p.total_writes) AS total_writes,
           SUM(p.lock_wait_count) AS lock_wait_count,
           SUM(p.lock_wait_ms) AS lock_wait_ms,
           SUM(p.latch_wait_count) AS latch_wait_count,
           SUM(p.latch_wait_ms) AS latch_wait_ms,
           SUM(r.total_rows) AS total_rows,
           MIN(p.uptime_days) AS uptime_days
    FROM per_db p
    LEFT JOIN db_rows r
           ON r.server_id = p.server_id AND r.database_name = p.database_name
    GROUP BY p.server_id, ROLLUP(p.database_name)
)
SELECT
    srv.name AS "Server",
    s.scope AS "Database",
    s.tables_analyzed AS "Tables",
    s.index_count AS "Indexes",
    round(s.total_size_gb, 2) AS "Size GB",
    s.total_rows AS "Rows",
    s.unused_indexes AS "Unused",
    round(s.unused_size_gb, 2) AS "Unused GB",
    s.compressable_indexes AS "Compressable",
    CASE WHEN s.is_overall = 1 THEN 'N/A'
         ELSE {n0('s.user_seeks + s.user_scans + s.user_lookups')}
              || ' (' || {n0('s.user_seeks')} || ' seeks, '
              || {n0('s.user_scans')} || ' scans, '
              || {n0('s.user_lookups')} || ' lookups)' END AS "Reads",
    CASE WHEN s.is_overall = 1 THEN 'N/A' ELSE {n0('s.total_writes')} END AS "Writes",
    CASE WHEN s.is_overall = 1 THEN 'N/A'
         ELSE {n0('s.lock_wait_count')} END AS "Lock Waits",
    CASE WHEN s.is_overall = 1 THEN 'N/A'
         WHEN s.lock_wait_count > 0
         THEN round(s.lock_wait_ms::numeric / s.lock_wait_count, 2)::text
         ELSE '0' END AS "Avg Lock Wait ms",
    CASE WHEN s.is_overall = 1 THEN 'N/A'
         ELSE {n0('s.latch_wait_count')} END AS "Latch Waits",
    CASE WHEN s.is_overall = 1 THEN 'N/A'
         WHEN s.latch_wait_count > 0
         THEN round(s.latch_wait_ms::numeric / s.latch_wait_count, 2)::text
         ELSE '0' END AS "Avg Latch Wait ms",
    CASE
        WHEN s.uptime_days IS NULL THEN 'Uptime unknown'
        WHEN s.uptime_days <= 7
        THEN 'Uptime ' || round(s.uptime_days, 1)
             || 'd - dedupe-only, unused indexes not reported'
        WHEN s.uptime_days < 14
        THEN 'Uptime ' || round(s.uptime_days, 1)
             || 'd - usage counters may be incomplete'
        ELSE ''
    END AS "Note"
FROM scoped s
{server_join('s.server_id')}
ORDER BY srv.name, s.is_overall DESC, s.scope
"""

_UNUSED_SQL = f"""
WITH {_SNAPSHOT}
SELECT
    srv.name AS "Server",
    a.database_name AS "Database",
    a.schema_name AS "Schema",
    a.table_name AS "Table",
    a.index_name AS "Index",
    CASE WHEN COALESCE(a.uptime_days, 999) < 14
         THEN 'Unused Index (uptime < 14 days)' ELSE 'Unused Index' END AS "Rule",
    round(COALESCE(a.reserved_mb, 0) / 1024.0, 3) AS "Size GB",
    a.total_rows AS "Rows",
    a.reads AS "Reads",
    COALESCE(a.user_updates, 0) AS "Writes",
    a.index_type_desc AS "Type",
    /* OriginalIndexDefinition's inputs. */
    a.key_columns AS "Key Columns",
    COALESCE(a.included_columns, '') AS "Included Columns",
    COALESCE(a.filter_definition, '') AS "Filter",
    COALESCE(a.data_compression_desc, '') AS "Compression",
    CASE WHEN a.can_compress THEN 'Yes' ELSE 'No' END AS "Compressable"
FROM analyzed a
{server_join('a.server_id')}
WHERE a.is_unused
ORDER BY a.database_name, a.schema_name, a.table_name, a.index_name
"""

_LIMITATION = """
## What the rule engine adds

Upstream also runs an ordered consolidation engine (`IndexCleanupAnalyzer`, reproducing
`sp_IndexCleanup`): exact duplicate, key subset, key superset, key duplicate and
unique-constraint replacement, each emitting a generated `DISABLE` / `MERGE` /
`DROP CONSTRAINT` script. Its output is destructive DDL, so it is not re-derived as panel SQL.

The rollup columns needing an engine verdict are therefore absent: **To Disable**,
**To Merge**, **Disable GB**, and the **compression-savings band**, which counts only indexes
the engine leaves alone.

For those recommendations and their scripts, use the PerformanceMonitor Viewer's
FinOps -> Index Analysis tab against this store, or run `sp_IndexCleanup` on the target.
"""


def index_analysis():
    """Build the FinOps Index Analysis dashboard."""
    reset_id()
    panels: list[dict] = []

    y = subtab(
        panels,
        "Reclaimable Space (overall + per database)",
        0,
        [
            (
                24,
                9,
                lambda x, y, w, h: table(
                    "Reclaimable Space",
                    x,
                    y,
                    w,
                    h,
                    _ROLLUP_SQL,
                    overrides=[
                        col_unit("Size GB", "gbytes"),
                        col_unit("Unused GB", "gbytes"),
                        col_thresholds("Unused", ("text", None), ("yellow", 1)),
                    ],
                    description=(
                        "From each server's latest snapshot - no live proc. ALL DATABASES "
                        "renders the workload columns N/A, as sp_IndexCleanup does."
                    ),
                ),
            )
        ],
    )

    y = subtab(
        panels,
        "Unused Indexes",
        y,
        [
            (
                24,
                12,
                lambda x, y, w, h: table(
                    "Unused Indexes",
                    x,
                    y,
                    w,
                    h,
                    _UNUSED_SQL,
                    overrides=[
                        col_unit("Size GB", "gbytes"),
                        status_colors("Compressable", {"Yes": "green", "No": "text"}),
                    ],
                    sort_by=[{"displayName": "Size GB", "desc": True}],
                    description=(
                        "Nonclustered, non-unique, non-PK/UC/FK-referenced indexes with no "
                        "reads. Empty on a server up 7 days or less, where upstream "
                        "switches to dedupe-only."
                    ),
                ),
            )
        ],
    )

    subtab(
        panels,
        "Consolidation Recommendations",
        y,
        [(24, 9, lambda x, y, w, h: text_panel("Not ported", x, y, w, h, _LIMITATION))],
    )

    return finops_dashboard(
        uid("finops-index-analysis"),
        "FinOps - Index Analysis",
        panels,
        [server_var()],
        refresh="15m",
    )
