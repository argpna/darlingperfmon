"""FinOps Workload & Contention dashboard - merges High Impact,
Application Connections, and Locking & Contention dashboards: what's driving cost and what's
contending for locks.

Upstream ref: ViewerDataService.FinOps.Workload.cs, .Locking.cs.

High Impact's score is the union of the top 10 rows by each of six dimensions, averaged as
percent ranks within that set (HighImpactScorer, ported to Postgres percent_rank()).
"""

from .._shared import (
    RAW_MAX_AGE_DAYS,
    UTC_NOW,
    col_gauge_bar,
    col_thresholds,
    col_unit,
    collector,
    finops_dashboard,
    multi_filter,
    query_var,
    reset_id,
    server_filter,
    server_join,
    server_var,
    subtab,
    table,
    uid,
)

_QUERY_STATS = collector("query_stats")
_SESSIONS = collector("session_stats")
_IOS = collector("index_object_stats")

# HighImpactScorer's topN per dimension.
_HIGH_IMPACT_TOP_N = 10

_DIMENSIONS = (
    ("total_cpu_ms", "cpu"),
    ("total_duration_ms", "duration"),
    ("total_reads", "reads"),
    ("total_writes", "writes"),
    ("total_memory_mb", "memory"),
    ("total_executions", "executions"),
)

_INTERESTING = "\nUNION\n".join(f"""    (SELECT server_id, query_hash,
            ROW_NUMBER() OVER (PARTITION BY server_id ORDER BY {col} DESC) AS rn
     FROM agg)""" for col, _ in _DIMENSIONS)

_RANKS = ",\n".join(
    f"           percent_rank() OVER (PARTITION BY server_id ORDER BY {col})"
    f" AS {name}_pctl"
    for col, name in _DIMENSIONS
)

_SHARES = ",\n".join(
    f"           round(100.0 * {col}"
    f" / NULLIF(SUM({col}) OVER (PARTITION BY server_id), 0), 1) AS {name}_share"
    for col, name in _DIMENSIONS
)

_SCORE = (
    "trunc((" + " + ".join(f"{name}_pctl" for _, name in _DIMENSIONS) + ") / 6.0 * 100)"
)

_HIGH_IMPACT_SQL = f"""
WITH agg AS (
    SELECT qs.server_id,
           qs.query_hash,
           MIN(qs.database_name) AS database_name,
           SUM(qs.delta_execution_count) AS total_executions,
           SUM(qs.delta_worker_time) / 1000.0 AS total_cpu_ms,
           SUM(qs.delta_elapsed_time) / 1000.0 AS total_duration_ms,
           SUM(qs.delta_logical_reads) AS total_reads,
           SUM(qs.delta_logical_writes) AS total_writes,
           SUM(COALESCE(qs.max_grant_kb, 0)) / 1024.0 AS total_memory_mb
    FROM {_QUERY_STATS} AS qs
    WHERE {server_filter('qs.server_id')}
      AND $__timeFilter(qs.collection_time)
      AND qs.query_hash IS NOT NULL AND qs.query_hash <> ''
      AND qs.delta_execution_count > 0
    GROUP BY qs.server_id, qs.query_hash
    HAVING SUM(qs.delta_execution_count) > 0
),
per_dimension AS (
{_INTERESTING}
),
interesting AS (
    SELECT DISTINCT server_id, query_hash FROM per_dimension WHERE rn <= {_HIGH_IMPACT_TOP_N}
),
filtered AS (
    SELECT a.* FROM agg a
    JOIN interesting i ON i.server_id = a.server_id AND i.query_hash = a.query_hash
),
scored AS (
    SELECT f.*,
{_RANKS},
{_SHARES}
    FROM filtered f
)
SELECT
    {_SCORE} AS "Score",
    srv.name AS "Server",
    s.database_name AS "Database",
    s.total_executions AS "Executions",
    s.executions_share AS "Executions %",
    round(s.total_cpu_ms, 0) AS "CPU (ms)",
    s.cpu_share AS "CPU %",
    round(s.total_duration_ms, 0) AS "Duration (ms)",
    s.duration_share AS "Duration %",
    s.total_reads AS "Reads",
    s.reads_share AS "Reads %",
    s.total_writes AS "Writes",
    s.writes_share AS "Writes %",
    round(s.total_memory_mb, 1) AS "Memory (MB)",
    s.memory_share AS "Memory %",
    left(txt.query_text, 200) AS "Query Preview",
    txt.query_text AS "Query Text"
FROM scored s
{server_join('s.server_id')}
LEFT JOIN LATERAL (
    /* Upstream's correlated sample-text subquery. */
    SELECT qs2.query_text
    FROM {_QUERY_STATS} AS qs2
    WHERE qs2.server_id = s.server_id
      AND qs2.query_hash = s.query_hash
      AND $__timeFilter(qs2.collection_time)
      AND qs2.query_text IS NOT NULL AND qs2.query_text <> ''
    ORDER BY qs2.delta_execution_count DESC NULLS LAST
    LIMIT 1
) txt ON true
ORDER BY 1 DESC
"""

# The AVG/MAX resource columns are nullable in the store, so they read NULL until the
# collector populates them - upstream shows the same blanks rather than inventing zeros.
_CONNECTIONS_SQL = f"""
SELECT
    srv.name AS "Server",
    s.program_name AS "Application",
    AVG(s.connection_count)::int AS "Avg Connections",
    MAX(s.connection_count) AS "Max Connections",
    AVG(s.running_count)::int AS "Avg Running",
    MAX(s.running_count) AS "Max Running",
    AVG(s.sleeping_count)::int AS "Avg Sleeping",
    MAX(s.sleeping_count) AS "Max Sleeping",
    AVG(s.dormant_count)::int AS "Avg Dormant",
    MAX(s.dormant_count) AS "Max Dormant",
    AVG(s.total_cpu_time_ms)::bigint AS "Avg CPU (ms)",
    MAX(s.total_cpu_time_ms) AS "Max CPU (ms)",
    AVG(s.total_reads)::bigint AS "Avg Reads",
    MAX(s.total_reads) AS "Max Reads",
    AVG(s.total_writes)::bigint AS "Avg Writes",
    MAX(s.total_writes) AS "Max Writes",
    AVG(s.total_logical_reads)::bigint AS "Avg Logical Reads",
    MAX(s.total_logical_reads) AS "Max Logical Reads",
    COUNT(*) AS "Samples",
    MIN(s.collection_time) AS "First Seen",
    MAX(s.collection_time) AS "Last Seen"
FROM {_SESSIONS} AS s
{server_join('s.server_id')}
WHERE {server_filter('s.server_id')}
  AND s.collection_time >= {UTC_NOW} - INTERVAL '24 hours'
GROUP BY srv.name, s.program_name
ORDER BY MAX(s.connection_count) DESC
"""

# IndexLockingAllSql's top-N.
_LOCKING_TOP_N = 200

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
_LOCKING_LATEST_PER_DB = f"""latest AS (
    SELECT server_id, database_name, MAX(collection_time) AS latest_time
    FROM {_IOS}
    WHERE {server_filter()}
    GROUP BY server_id, database_name
)"""

_LOCKING_DB_VAR_SQL = f"""
WITH {_LOCKING_LATEST_PER_DB}
SELECT DISTINCT ios.database_name
FROM {_IOS} AS ios
JOIN latest l ON l.server_id = ios.server_id
             AND l.database_name = ios.database_name
             AND l.latest_time = ios.collection_time
WHERE {server_filter('ios.server_id')} AND {_HAS_CONTENTION}
ORDER BY ios.database_name
"""

_LOCKING_SQL = f"""
WITH {_LOCKING_LATEST_PER_DB},
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
WHERE c.rn <= {_LOCKING_TOP_N}
ORDER BY c.total_wait_ms DESC
"""


def workload_contention():
    """Build the FinOps Workload & Contention dashboard."""
    reset_id()
    panels: list[dict] = []

    y = subtab(
        panels,
        "High Impact Queries",
        0,
        [
            (
                24,
                20,
                lambda x, y, w, h: table(
                    "High Impact Queries",
                    x,
                    y,
                    w,
                    h,
                    _HIGH_IMPACT_SQL,
                    overrides=[
                        # ImpactScoreColor - a high score is the alarming one.
                        col_thresholds(
                            "Score", ("green", None), ("yellow", 60), ("red", 80)
                        ),
                        col_unit("CPU (ms)", "ms"),
                        col_unit("Duration (ms)", "ms"),
                        col_unit("Memory (MB)", "mbytes"),
                        col_gauge_bar("CPU %"),
                        col_gauge_bar("Duration %"),
                        col_gauge_bar("Reads %"),
                        col_gauge_bar("Writes %"),
                        col_gauge_bar("Memory %"),
                        col_gauge_bar("Executions %"),
                    ],
                    sort_by=[{"displayName": "Score", "desc": True}],
                    description=(
                        f"Union of the top {_HIGH_IMPACT_TOP_N} by each of CPU, duration, "
                        "reads, writes, memory and executions; shares and ranks are "
                        f"computed within that set. Rows thin out beyond {RAW_MAX_AGE_DAYS} "
                        "days, where query text is no longer retained."
                    ),
                ),
            )
        ],
    )

    y = subtab(
        panels,
        "Application Connections",
        y,
        [
            (
                24,
                20,
                lambda x, y, w, h: table(
                    "Application Connections (24h)",
                    x,
                    y,
                    w,
                    h,
                    _CONNECTIONS_SQL,
                    overrides=[
                        col_unit("Avg CPU (ms)", "ms"),
                        col_unit("Max CPU (ms)", "ms"),
                    ],
                    sort_by=[{"displayName": "Max Connections", "desc": True}],
                    description=(
                        "Fixed 24h window, as upstream's is - the range picker does not "
                        "move it."
                    ),
                ),
            )
        ],
    )

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

    subtab(
        panels,
        "Locking & Contention",
        y,
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
                    _LOCKING_SQL,
                    overrides=wait_gauges + [col_unit("Reserved MB", "mbytes")],
                    sort_by=[{"displayName": "Row Lock Wait ms", "desc": True}],
                    description=(
                        f"Top {_LOCKING_TOP_N} indexes per server by total lock + latch "
                        "wait, at each database's latest snapshot. Lifetime totals, not "
                        "rates."
                    ),
                ),
            )
        ],
    )

    return finops_dashboard(
        uid("finops-workload-contention"),
        "Workload & Contention",
        panels,
        [
            server_var(),
            query_var(
                "database",
                "Database",
                _LOCKING_DB_VAR_SQL,
                "Databases with recorded lock or latch contention.",
            ),
        ],
    )
