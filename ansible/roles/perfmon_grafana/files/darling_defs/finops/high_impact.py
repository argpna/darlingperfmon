"""FinOps High Impact dashboard (Darling line).

Upstream ref: HighImpactQueriesSql (ViewerDataService.FinOps.Workload.cs), HighImpactScorer
(ViewerDataService.FinOps.cs).

HighImpactScorer is ported to SQL: keep the union of the top 10 by each of six dimensions,
then score each row as the mean of its six percent ranks within that set. Postgres'
percent_rank() is upstream's PercentRank exactly. Every step partitions by server.
"""

from .._shared import (
    RAW_MAX_AGE_DAYS,
    col_gauge_bar,
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

_QUERY_STATS = collector("query_stats")

# HighImpactScorer's topN per dimension.
_TOP_N = 10

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

_SQL = f"""
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
    SELECT DISTINCT server_id, query_hash FROM per_dimension WHERE rn <= {_TOP_N}
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


def high_impact():
    """Build the FinOps High Impact dashboard."""
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
                    "High Impact Queries",
                    x,
                    y,
                    w,
                    h,
                    _SQL,
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
                        f"Union of the top {_TOP_N} by each of CPU, duration, reads, "
                        "writes, memory and executions; shares and ranks are computed "
                        f"within that set. Rows thin out beyond {RAW_MAX_AGE_DAYS} days, "
                        "where query text is no longer retained."
                    ),
                ),
            )
        ],
    )

    return finops_dashboard(
        uid("finops-high-impact"),
        "FinOps - High Impact",
        panels,
        [server_var()],
    )
