"""FinOps Application Connections dashboard (Darling line).

Upstream ref: ApplicationConnectionsSql (ViewerDataService.FinOps.Workload.cs).
"""

from .._shared import (
    UTC_NOW,
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

_SESSIONS = collector("session_stats")

# The AVG/MAX resource columns are nullable in the store, so they read NULL until the
# collector populates them - upstream shows the same blanks rather than inventing zeros.
_SQL = f"""
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


def application_connections():
    """Build the FinOps Application Connections dashboard."""
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
                    "Application Connections (24h)",
                    x,
                    y,
                    w,
                    h,
                    _SQL,
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

    return finops_dashboard(
        uid("finops-application-connections"),
        "FinOps - Application Connections",
        panels,
        [server_var()],
    )
