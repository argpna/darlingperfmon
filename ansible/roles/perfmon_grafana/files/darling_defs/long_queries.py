"""Long Queries dashboard (Darling line, #1496).

Upstream ref: ViewerServerTab.LongQueries.cs, ViewerDataService.LongQueries.cs. Single
flat tab: completed long-running queries plus attentions (cancels/timeouts) from the
opt-in PerformanceMonitor_LongQueryCompletions XE session, reading the base
long_query_completions table (no collect.v_* view for this collector).

The collector defaults OFF, so a Trace Status panel surfaces the resolved per-server
enabled state in place of the WPF tab's hide/show banner - a table generalizes better
than a single banner now that $server is multi-select. Duration/CPU are shown in
milliseconds (divided down from stored microseconds) for numeric sortability.
"""

from ._shared import (
    col_unit,
    collector,
    dashboard,
    flow,
    multi_filter,
    query_var,
    reset_id,
    server_filter,
    server_join,
    server_var,
    SERVER_REGISTRY,
    status_colors,
    table,
    uid,
)

_DATABASE_VAR_SQL = f"""
SELECT DISTINCT database_name
FROM {collector('long_query_completions')}
WHERE {server_filter()}
ORDER BY 1
"""

# Upstream ref: GetLongQueryTraceEnabledAsync - per-server override > fleet override > default OFF.
_TRACE_STATUS_SQL = f"""
SELECT
    srv.name AS "Server",
    CASE WHEN COALESCE(
        (SELECT s.enabled FROM config.config_collector_schedules AS s
         WHERE s.collector_name = 'long_query_completions'
           AND s.server_id = srv.server_id),
        (SELECT s.enabled FROM config.config_collector_schedules AS s
         WHERE s.collector_name = 'long_query_completions'
           AND s.server_id IS NULL),
        false
    ) THEN 'ON' ELSE 'OFF' END AS "Trace Status"
FROM {SERVER_REGISTRY} AS srv
WHERE srv.is_enabled
  AND {server_filter('srv.server_id')}
ORDER BY srv.name
"""

# Upstream ref: LongQueryCompletionsSql. IsAborted/IsAttention row tints become column colors below.
_LONG_QUERIES_SQL = f"""
SELECT
    srv.name AS "Server",
    lqc.event_time AS "Event Time",
    lqc.event_type AS "Event Type",
    lqc.duration_microseconds / 1000.0 AS "Duration",
    lqc.cpu_time_microseconds / 1000.0 AS "CPU",
    lqc.logical_reads AS "Logical Reads",
    lqc.physical_reads AS "Physical Reads",
    lqc.writes AS "Writes",
    lqc.row_count AS "Rows",
    lqc.result AS "Result",
    lqc.database_name AS "Database",
    lqc.object_name AS "Object",
    lqc.statement_text AS "Statement",
    lqc.session_id AS "Session",
    lqc.client_app_name AS "Application",
    lqc.server_principal_name AS "Login",
    lqc.query_hash AS "Query Hash"
FROM {collector('long_query_completions')} AS lqc
{server_join('lqc.server_id')}
WHERE $__timeFilter(lqc.collection_time)
  AND {server_filter('lqc.server_id')}
  AND {multi_filter('lqc.database_name', 'database')}
ORDER BY lqc.event_time DESC
LIMIT 200
"""


def long_queries():
    """Build the Long Queries dashboard."""
    reset_id()
    panels: list[dict] = []

    flow(
        panels,
        0,
        [
            (
                24,
                5,
                lambda x, y, w, h: table(
                    "Trace Status",
                    x,
                    y,
                    w,
                    h,
                    _TRACE_STATUS_SQL,
                    overrides=[
                        status_colors("Trace Status", {"ON": "green", "OFF": "red"}),
                    ],
                    description="Opt-in collector, defaults OFF. Enable it in the collector schedule config.",
                ),
            ),
            (
                24,
                16,
                lambda x, y, w, h: table(
                    "Long-Running Query Completions",
                    x,
                    y,
                    w,
                    h,
                    _LONG_QUERIES_SQL,
                    sort_by=[{"displayName": "Event Time", "desc": True}],
                    overrides=[
                        status_colors("Result", {"Abort": "red"}),
                        status_colors("Event Type", {"attention": "orange"}),
                        col_unit("Duration", "ms"),
                        col_unit("CPU", "ms"),
                    ],
                    description="Attentions (cancels/timeouts) carry no duration - the event has none.",
                ),
            ),
        ],
    )

    return dashboard(
        uid("long-queries"),
        "Long Queries",
        panels,
        [
            server_var(),
            query_var(
                "database",
                "Database",
                _DATABASE_VAR_SQL,
                "Optional database filter.",
            ),
        ],
    )
