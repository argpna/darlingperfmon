"""FinOps Optimization & Indexing dashboard (Darling line) - merges Optimization and
Index Analysis dashboards into one cleanup-opportunities page.

Upstream ref: ViewerDataService.FinOps.Storage.cs, .Workload.cs, .Utilization.cs,
.IndexAnalysis.cs, IndexCleanupAnalyzer.cs.

Wait Stats Summary and Expensive Queries follow the dashboard range; the rest of Optimization
keeps upstream's fixed windows. Index Analysis ports the analyzer's snapshot-derived half only
- see its own text panel for what the consolidation-rule engine leaves out.
"""

from .._shared import (
    RAW_MAX_AGE_DAYS,
    UTC_NOW,
    col_gauge_bar,
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
from ._shared import (
    IDLE_DAYS,
    budget_cte,
    fixed_window_tiers,
    idle_db_ctes,
    wait_category,
    window_budget,
)

_TEMPDB = collector("tempdb_stats")
_WAITS = collector("wait_stats")
_QUERY_STATS = collector("query_stats")
_GRANTS = collector("memory_grant_stats")
_IOS = collector("index_object_stats")
_PROPS = collector("server_properties")


def _idle_sql(relation: str, time_col: str) -> str:
    """The idle-database grid for one retention tier."""
    return f"""
WITH {idle_db_ctes(relation, time_col, with_details=True)}
SELECT server_id, database_name, total_size_mb, file_count, last_execution
FROM idle_dbs
"""


_IDLE_SQL = f"""
WITH i AS ({fixed_window_tiers(_idle_sql, IDLE_DAYS)})
SELECT
    srv.name AS "Server",
    i.database_name AS "Database",
    round(i.total_size_mb, 2) AS "Total Size MB",
    i.file_count AS "Files",
    i.last_execution AS "Last Execution"
FROM i
{server_join('i.server_id')}
ORDER BY i.total_size_mb DESC
"""

# TempdbSummarySql. The VALUES spine preserves upstream's metric order.
_TEMPDB_SQL = f"""
WITH latest AS (
    SELECT DISTINCT ON (server_id)
           server_id, user_object_reserved_mb, internal_object_reserved_mb,
           version_store_reserved_mb, total_reserved_mb
    FROM {_TEMPDB}
    WHERE {server_filter()}
    ORDER BY server_id, collection_time DESC
),
peak AS (
    SELECT server_id,
           MAX(user_object_reserved_mb) AS max_user_mb,
           MAX(internal_object_reserved_mb) AS max_internal_mb,
           MAX(version_store_reserved_mb) AS max_version_store_mb,
           MAX(total_reserved_mb) AS max_total_mb
    FROM {_TEMPDB}
    WHERE {server_filter()} AND collection_time >= {UTC_NOW} - INTERVAL '24 hours'
    GROUP BY server_id
)
SELECT
    srv.name AS "Server",
    m.metric AS "Metric",
    round(m.current_mb, 2) AS "Current MB",
    round(m.peak_mb, 2) AS "Peak 24h MB",
    m.warning AS "Warning"
FROM latest l
JOIN peak p ON p.server_id = l.server_id
{server_join('l.server_id')}
CROSS JOIN LATERAL (VALUES
    ('User Objects', l.user_object_reserved_mb, p.max_user_mb,
     CASE WHEN p.max_user_mb > 1024 THEN 'High user object usage' ELSE '' END),
    ('Internal Objects', l.internal_object_reserved_mb, p.max_internal_mb,
     CASE WHEN p.max_internal_mb > 1024
          THEN 'High internal object usage (sorts/hashes)' ELSE '' END),
    ('Version Store', l.version_store_reserved_mb, p.max_version_store_mb,
     CASE WHEN p.max_version_store_mb > 2048
          THEN 'Version store pressure - check long-running transactions' ELSE '' END),
    ('Total Reserved', l.total_reserved_mb, p.max_total_mb, '')
) AS m(metric, current_mb, peak_mb, warning)
ORDER BY srv.name, m.metric
"""

_CATEGORY = wait_category("ws.wait_type")

_WAITS_SQL = f"""
WITH categorized AS (
    SELECT ws.server_id,
           {_CATEGORY} AS category,
           ws.wait_type,
           SUM(ws.delta_wait_time_ms) AS wait_time_ms,
           SUM(ws.delta_waiting_tasks) AS waiting_tasks
    FROM {_WAITS} AS ws
    WHERE {server_filter('ws.server_id')}
      AND $__timeFilter(ws.collection_time)
      AND ws.delta_wait_time_ms IS NOT NULL
      AND ws.delta_wait_time_ms > 0
    GROUP BY ws.server_id, {_CATEGORY}, ws.wait_type
),
ranked AS (
    SELECT c.*,
           ROW_NUMBER() OVER (
               PARTITION BY c.server_id, c.category ORDER BY c.wait_time_ms DESC) AS rn
    FROM categorized c
),
by_category AS (
    SELECT server_id, category,
           SUM(wait_time_ms) AS total_wait_time_ms,
           SUM(waiting_tasks) AS total_waiting_tasks,
           MAX(CASE WHEN rn = 1 THEN wait_type END) AS top_wait_type,
           MAX(CASE WHEN rn = 1 THEN wait_time_ms END) AS top_wait_time_ms
    FROM ranked
    GROUP BY server_id, category
),
grand_total AS (
    SELECT server_id, NULLIF(SUM(total_wait_time_ms), 0) AS total
    FROM by_category
    GROUP BY server_id
),
{budget_cte()}
SELECT
    srv.name AS "Server",
    bc.category AS "Category",
    bc.total_wait_time_ms AS "Total Wait ms",
    bc.total_waiting_tasks AS "Waiting Tasks",
    round(bc.total_wait_time_ms * 100.0 / gt.total, 1) AS "% of Total",
    bc.top_wait_type AS "Top Wait Type",
    bc.top_wait_time_ms AS "Top Wait ms",
    round(COALESCE(
        bc.total_wait_time_ms::numeric / gt.total * {window_budget()}, 0), 2)
        AS "Est. Cost ($)"
FROM by_category bc
JOIN grand_total gt ON gt.server_id = bc.server_id
LEFT JOIN budget b ON b.server_id = bc.server_id
{server_join('bc.server_id')}
ORDER BY bc.total_wait_time_ms DESC
"""

# ExpensiveQueriesSql's topN.
_EXPENSIVE_TOP_N = 20

# Query text lives only in raw, so this panel cannot route. Upstream clamps to RawTextHorizon
# and labels the clamp; the GREATEST below is that clamp.
_TEXT_HORIZON = f"{UTC_NOW} - INTERVAL '{RAW_MAX_AGE_DAYS} days'"

_EXPENSIVE_SQL = f"""
WITH top_queries AS (
    SELECT qs.server_id,
           qs.database_name,
           SUM(qs.delta_worker_time) / 1000.0 AS total_cpu_ms,
           SUM(qs.delta_worker_time) / 1000.0
               / NULLIF(SUM(qs.delta_execution_count), 0) AS avg_cpu_ms,
           SUM(qs.delta_logical_reads) AS total_reads,
           SUM(qs.delta_logical_reads)::numeric
               / NULLIF(SUM(qs.delta_execution_count), 0) AS avg_reads,
           SUM(qs.delta_execution_count) AS executions,
           left(qs.query_text, 200) AS query_preview,
           qs.query_text AS full_query_text,
           ROW_NUMBER() OVER (
               PARTITION BY qs.server_id
               ORDER BY SUM(qs.delta_worker_time) DESC) AS rn
    FROM {_QUERY_STATS} AS qs
    WHERE {server_filter('qs.server_id')}
      AND qs.collection_time >= GREATEST($__timeFrom()::timestamp, {_TEXT_HORIZON})
      AND qs.collection_time <= $__timeTo()::timestamp
      AND qs.delta_worker_time IS NOT NULL
      AND qs.delta_worker_time > 0
    GROUP BY qs.server_id, qs.database_name, qs.sql_handle, qs.query_text
),
kept AS (
    SELECT * FROM top_queries WHERE rn <= {_EXPENSIVE_TOP_N}
),
totals AS (
    SELECT server_id, NULLIF(SUM(total_cpu_ms), 0) AS total_cpu
    FROM kept
    GROUP BY server_id
),
{budget_cte()}
SELECT
    srv.name AS "Server",
    k.database_name AS "Database",
    round(k.total_cpu_ms, 0) AS "Total CPU ms",
    round(k.avg_cpu_ms, 2) AS "Avg CPU/Exec",
    k.total_reads AS "Total Reads",
    round(k.avg_reads, 0) AS "Avg Reads/Exec",
    k.executions AS "Executions",
    round(COALESCE(k.total_cpu_ms / t.total_cpu * {window_budget()}, 0), 2)
        AS "Est. Cost ($)",
    k.query_preview AS "Query Preview",
    k.full_query_text AS "Query Text"
FROM kept k
JOIN totals t ON t.server_id = k.server_id
LEFT JOIN budget b ON b.server_id = k.server_id
{server_join('k.server_id')}
ORDER BY k.total_cpu_ms DESC
"""

# MemoryGrantEfficiencySql. WastedMb is a computed property upstream, not a column. Note the
# _delta SUFFIX on the two cumulative columns; the rest are point-in-time gauges.
_GRANTS_SQL = f"""
SELECT
    mg.collection_time::date AS "Day",
    srv.name AS "Server",
    round(AVG(mg.granted_memory_mb), 1) AS "Avg Granted MB",
    round(AVG(mg.used_memory_mb), 1) AS "Avg Used MB",
    round(AVG(mg.used_memory_mb) * 100.0 / NULLIF(AVG(mg.granted_memory_mb), 0), 1)
        AS "Efficiency %",
    round(MAX(mg.granted_memory_mb), 1) AS "Peak Grant MB",
    round(AVG(mg.granted_memory_mb) - AVG(mg.used_memory_mb), 1) AS "Wasted MB",
    SUM(mg.grantee_count) AS "Grantees",
    SUM(mg.waiter_count) AS "Waiters",
    SUM(mg.timeout_error_count_delta) AS "Timeouts",
    SUM(mg.forced_grant_count_delta) AS "Forced"
FROM {_GRANTS} AS mg
{server_join('mg.server_id')}
WHERE {server_filter('mg.server_id')}
  AND mg.collection_time >= {UTC_NOW} - INTERVAL '24 hours'
GROUP BY mg.collection_time::date, srv.name
ORDER BY mg.collection_time::date
"""

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

_INDEX_READS = (
    "COALESCE(ios.user_seeks, 0) + COALESCE(ios.user_scans, 0)"
    " + COALESCE(ios.user_lookups, 0)"
)

# Rule 1 (ApplyUnused), verbatim.
_IS_UNUSED = f"""({_INDEX_READS} = 0
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
           {_INDEX_READS} AS reads,
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


def optimization_indexing():
    """Build the FinOps Optimization & Indexing dashboard."""
    reset_id()
    panels: list[dict] = []

    y = subtab(
        panels,
        "Idle Databases",
        0,
        [
            (
                24,
                8,
                lambda x, y, w, h: table(
                    f"Idle Databases (no activity in {IDLE_DAYS} days)",
                    x,
                    y,
                    w,
                    h,
                    _IDLE_SQL,
                    overrides=[col_unit("Total Size MB", "mbytes")],
                    sort_by=[{"displayName": "Total Size MB", "desc": True}],
                    description=(
                        f"Fixed {IDLE_DAYS}-day window. System databases and "
                        "PerformanceMonitor are never counted. Last Execution is the "
                        "monitored server's local clock."
                    ),
                ),
            )
        ],
    )

    y = subtab(
        panels,
        "tempdb Pressure",
        y,
        [
            (
                24,
                8,
                lambda x, y, w, h: table(
                    "tempdb Pressure",
                    x,
                    y,
                    w,
                    h,
                    _TEMPDB_SQL,
                    overrides=[
                        col_unit("Current MB", "mbytes"),
                        col_unit("Peak 24h MB", "mbytes"),
                    ],
                ),
            )
        ],
    )

    y = subtab(
        panels,
        "Wait Stats Summary",
        y,
        [
            (
                24,
                8,
                lambda x, y, w, h: table(
                    "Wait Stats Summary",
                    x,
                    y,
                    w,
                    h,
                    _WAITS_SQL,
                    overrides=[
                        col_unit("Total Wait ms", "ms"),
                        col_unit("Top Wait ms", "ms"),
                        col_gauge_bar("% of Total"),
                        col_unit("Est. Cost ($)", "currencyUSD"),
                    ],
                    sort_by=[{"displayName": "Total Wait ms", "desc": True}],
                    description=(
                        "Est. Cost is the category's share of the budget, prorated to the "
                        "dashboard range."
                    ),
                ),
            )
        ],
    )

    y = subtab(
        panels,
        f"Expensive Queries (Top {_EXPENSIVE_TOP_N} by CPU)",
        y,
        [
            (
                24,
                10,
                lambda x, y, w, h: table(
                    f"Expensive Queries (Top {_EXPENSIVE_TOP_N} by CPU)",
                    x,
                    y,
                    w,
                    h,
                    _EXPENSIVE_SQL,
                    overrides=[
                        col_unit("Total CPU ms", "ms"),
                        col_unit("Avg CPU/Exec", "ms"),
                        col_unit("Est. Cost ($)", "currencyUSD"),
                    ],
                    sort_by=[{"displayName": "Total CPU ms", "desc": True}],
                    description=(
                        f"Query text lives only in the raw tier, so this reaches back at "
                        f"most {RAW_MAX_AGE_DAYS} days however wide the range. Upstream's "
                        "stored-plan viewer has no Grafana equivalent."
                    ),
                ),
            )
        ],
    )

    y = subtab(
        panels,
        "Memory Grant Efficiency",
        y,
        [
            (
                24,
                8,
                lambda x, y, w, h: table(
                    "Memory Grant Efficiency",
                    x,
                    y,
                    w,
                    h,
                    _GRANTS_SQL,
                    overrides=[
                        col_unit("Avg Granted MB", "mbytes"),
                        col_unit("Avg Used MB", "mbytes"),
                        col_unit("Peak Grant MB", "mbytes"),
                        col_unit("Wasted MB", "mbytes"),
                        col_gauge_bar("Efficiency %"),
                        col_thresholds("Timeouts", ("text", None), ("red", 1)),
                        col_thresholds("Forced", ("text", None), ("yellow", 1)),
                    ],
                    sort_by=[{"displayName": "Day", "desc": True}],
                ),
            )
        ],
    )

    y = subtab(
        panels,
        "Reclaimable Space (overall + per database)",
        y,
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
        uid("finops-optimization-indexing"),
        "Optimization & Indexing",
        panels,
        [server_var()],
        refresh="15m",
    )
