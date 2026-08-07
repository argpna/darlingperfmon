"""Blocking & Deadlocks dashboard.

Upstream ref: ViewerServerTab.Blocking.cs, ViewerDataService.Blocking.cs,
ViewerDataService.BlockingStats.cs, ViewerDataService.BlockingTrends.cs,
ViewerDataService.BlockingSlicers.cs, ViewerServerTab.Deadlock.cs,
ViewerDataService.Deadlock.cs. Five sub-tabs: Trends, Blocking Stats, Current Waits,
Blocked Process Reports, Deadlocks.

No upstream ref: the hourly time-range slicer above the Blocked Process Reports /
Deadlocks grids is a WPF-only brush control; Grafana's own dashboard time-range picker
already does that job, so it has no separate panel here.

Upstream ref: OpenDeadlockGraphAsync (ViewerServerTab.Deadlock.cs) opens a graph viewer
on a deadlock row. Grafana has no XDL renderer, so deadlock_detail() below substitutes a
per-event participant grid plus the raw XML, reached via a data link from Deadlocks.
"""

from ._shared import (
    col_datalink,
    col_hidden,
    col_thresholds,
    collector,
    dashboard,
    detail_dashboard,
    multi_filter,
    query_var,
    reset_id,
    server_filter,
    server_join,
    server_var,
    stat_grid,
    subtab,
    table,
    target,
    text_var,
    thresholds,
    timeseries,
    uid,
)

# Upstream ref: GetLockWaitTrendAsync (ViewerDataService.BlockingTrends.cs)
_LOCK_WAIT_TREND_SQL = f"""
WITH windowed AS (
    SELECT
        ws.server_id,
        ws.wait_type,
        ws.collection_time,
        ws.delta_wait_time_ms,
        EXTRACT(EPOCH FROM (date_trunc('second', ws.collection_time)
            - date_trunc('second', LAG(ws.collection_time) OVER (
                PARTITION BY ws.server_id, ws.wait_type
                ORDER BY ws.collection_time)))) AS interval_seconds
    FROM {collector('wait_stats')} AS ws
    WHERE $__timeFilter(ws.collection_time)
      AND {server_filter('ws.server_id')}
      AND ws.wait_type LIKE 'LCK%'
)
SELECT
    collection_time AS time,
    wait_type AS metric,
    CASE
        WHEN interval_seconds > 0 AND delta_wait_time_ms >= 0
        THEN delta_wait_time_ms::double precision / interval_seconds
        ELSE 0
    END AS value
FROM windowed
ORDER BY 1
"""

# XE (blocked_process_reports) is preferred; the DMV snapshot poll only fills in when the
# XE trace produced nothing at all in this window, matching upstream's mutually exclusive
# fallback (a server runs one collector or the other, not both at once).
# Upstream ref: GetBlockingTrendAsync (ViewerDataService.BlockingTrends.cs)
_BLOCKING_TREND_SQL = f"""
WITH xe AS (
    SELECT bpr.server_id, date_trunc('minute', bpr.event_time) AS t, COUNT(*) AS c
    FROM {collector('blocked_process_reports')} AS bpr
    WHERE $__timeFilter(bpr.collection_time)
      AND {server_filter('bpr.server_id')}
      AND {multi_filter('bpr.database_name', 'database')}
    GROUP BY 1, 2
),
dmv AS (
    SELECT d.server_id, date_trunc('minute', d.event_time) AS t, COUNT(*) AS c
    FROM {collector('dmv_blocking_snapshots')} AS d
    WHERE $__timeFilter(d.collection_time)
      AND {server_filter('d.server_id')}
      AND {multi_filter('d.database_name', 'database')}
      AND NOT EXISTS (SELECT 1 FROM xe)
    GROUP BY 1, 2
)
SELECT b.t AS time, srv.name AS metric, b.c AS value
FROM (SELECT * FROM xe UNION ALL SELECT * FROM dmv) AS b
{server_join('b.server_id')}
ORDER BY 1
"""

# Upstream ref: GetDeadlockTrendAsync (ViewerDataService.BlockingTrends.cs)
_DEADLOCK_TREND_SQL = f"""
SELECT
    date_trunc('minute', dl.deadlock_time) AS time,
    srv.name AS metric,
    COUNT(*) AS value
FROM {collector('deadlocks')} AS dl
{server_join('dl.server_id')}
WHERE $__timeFilter(dl.collection_time)
  AND {server_filter('dl.server_id')}
GROUP BY 1, 2
ORDER BY 1
"""

# Same XE-preferred/DMV-fallback source as the Blocking Trend count, aggregated for
# duration instead of just COUNT(*).
# Upstream ref: GetBlockingDurationStatsAsync (ViewerDataService.BlockingStats.cs)
_BLOCKING_DURATION_SQL = f"""
WITH xe AS (
    SELECT bpr.server_id, date_trunc('minute', bpr.event_time) AS t,
           COUNT(*) AS c, SUM(bpr.wait_time_ms) AS sum_ms,
           MAX(bpr.wait_time_ms) AS max_ms, AVG(bpr.wait_time_ms) AS avg_ms
    FROM {collector('blocked_process_reports')} AS bpr
    WHERE $__timeFilter(bpr.collection_time)
      AND {server_filter('bpr.server_id')}
      AND {multi_filter('bpr.database_name', 'database')}
    GROUP BY 1, 2
),
dmv AS (
    SELECT d.server_id, date_trunc('minute', d.event_time) AS t,
           COUNT(*) AS c, SUM(d.wait_time_ms) AS sum_ms,
           MAX(d.wait_time_ms) AS max_ms, AVG(d.wait_time_ms) AS avg_ms
    FROM {collector('dmv_blocking_snapshots')} AS d
    WHERE $__timeFilter(d.collection_time)
      AND {server_filter('d.server_id')}
      AND {multi_filter('d.database_name', 'database')}
      AND NOT EXISTS (SELECT 1 FROM xe)
    GROUP BY 1, 2
)
SELECT * FROM xe
UNION ALL
SELECT * FROM dmv
"""

_BLOCKING_DURATION_MAX_AVG_SQL = f"""
WITH b AS ({_BLOCKING_DURATION_SQL})
SELECT b.t AS time, srv.name || ' - Max' AS metric, b.max_ms AS value
FROM b {server_join('b.server_id')}
UNION ALL
SELECT b.t, srv.name || ' - Avg', b.avg_ms
FROM b {server_join('b.server_id')}
ORDER BY 1
"""

_BLOCKING_TOTAL_DURATION_SQL = f"""
WITH b AS ({_BLOCKING_DURATION_SQL})
SELECT b.t AS time, srv.name AS metric, b.sum_ms AS value
FROM b {server_join('b.server_id')}
ORDER BY 1
"""

# Every process's wait_time_ms inside each deadlock's graph, not just the victim's -
# matching upstream's GetDeadlockSeverityStatsAsync, which parses deadlock_graph_xml in
# C# rather than reading a stored column. Postgres xpath() ports the same extraction.
# One quirk verified against live deadlock_graph_xml samples: after unnest(xpath(...)),
# a relative path ("@attr", "./@attr") no longer resolves against the extracted fragment -
# use an absolute path from the new implicit root ("/*/@attr") instead.
# Upstream ref: GetDeadlockSeverityStatsAsync (ViewerDataService.BlockingStats.cs)
_DEADLOCK_PROCESS_WAITS_SQL = f"""
WITH graphs AS (
    SELECT dl.server_id, dl.deadlock_time, dl.deadlock_graph_xml::xml AS graph
    FROM {collector('deadlocks')} AS dl
    WHERE $__timeFilter(dl.collection_time)
      AND {server_filter('dl.server_id')}
),
processes AS (
    SELECT
        g.server_id,
        g.deadlock_time,
        (xpath('/*/@waittime', proc))[1]::text::bigint AS wait_ms
    FROM graphs g
    CROSS JOIN LATERAL unnest(xpath('/deadlock/process-list/process', g.graph)) AS proc
)
SELECT
    date_trunc('minute', p.deadlock_time) AS t,
    p.server_id,
    COUNT(*) AS victim_count,
    SUM(p.wait_ms) AS sum_ms,
    MAX(p.wait_ms) AS max_ms,
    AVG(p.wait_ms) AS avg_ms
FROM processes p
GROUP BY 1, 2
"""

_DEADLOCK_WAIT_MAX_AVG_SQL = f"""
WITH d AS ({_DEADLOCK_PROCESS_WAITS_SQL})
SELECT d.t AS time, srv.name || ' - Max' AS metric, d.max_ms AS value
FROM d {server_join('d.server_id')}
UNION ALL
SELECT d.t, srv.name || ' - Avg', d.avg_ms
FROM d {server_join('d.server_id')}
ORDER BY 1
"""

_DEADLOCK_TOTAL_WAIT_SQL = f"""
WITH d AS ({_DEADLOCK_PROCESS_WAITS_SQL})
SELECT d.t AS time, srv.name AS metric, d.sum_ms AS value
FROM d {server_join('d.server_id')}
ORDER BY 1
"""

# Upstream ref: UpdateBlockingStatsSummary (ViewerServerTab.Blocking.cs) - durations are
# event-weighted (total / event count), not a mean of per-minute averages.
_BLOCKING_STATS_SUMMARY_DURATION = [
    {
        "title": "Blocking Events",
        "sql": f"""
            WITH b AS ({_BLOCKING_DURATION_SQL})
            SELECT COALESCE(SUM(c), 0) AS v FROM b
        """,
        "th": thresholds(("green", None), ("yellow", 1), ("red", 20)),
    },
    {
        "title": "Total Duration",
        "sql": f"""
            WITH b AS ({_BLOCKING_DURATION_SQL})
            SELECT COALESCE(SUM(sum_ms), 0) AS v FROM b
        """,
        "unit": "ms",
        "th": thresholds(("green", None)),
    },
    {
        "title": "Max Duration",
        "sql": f"""
            WITH b AS ({_BLOCKING_DURATION_SQL})
            SELECT COALESCE(MAX(max_ms), 0) AS v FROM b
        """,
        "unit": "ms",
        "th": thresholds(("green", None), ("red", 30000)),
    },
    {
        "title": "Avg Duration",
        "sql": f"""
            WITH b AS ({_BLOCKING_DURATION_SQL})
            SELECT CASE WHEN SUM(c) > 0 THEN SUM(sum_ms) / SUM(c) ELSE 0 END AS v FROM b
        """,
        "unit": "ms",
        "th": thresholds(("green", None), ("red", 30000)),
    },
]

_BLOCKING_STATS_SUMMARY_DEADLOCK = [
    {
        "title": "Deadlocks",
        "sql": f"""
            SELECT COUNT(*) AS v FROM {collector('deadlocks')} AS dl
            WHERE $__timeFilter(dl.collection_time) AND {server_filter('dl.server_id')}
        """,
        "th": thresholds(("green", None), ("yellow", 1), ("red", 5)),
    },
    {
        "title": "Deadlock Victims",
        "sql": f"""
            WITH d AS ({_DEADLOCK_PROCESS_WAITS_SQL})
            SELECT COALESCE(SUM(victim_count), 0) AS v FROM d
        """,
        "th": thresholds(("green", None), ("yellow", 1), ("red", 5)),
    },
    {
        "title": "Deadlock Wait",
        "sql": f"""
            WITH d AS ({_DEADLOCK_PROCESS_WAITS_SQL})
            SELECT COALESCE(SUM(sum_ms), 0) AS v FROM d
        """,
        "unit": "ms",
        "th": thresholds(("green", None)),
    },
]

# Base collect.waiting_tasks (a DMV-poll snapshot table, no *_delta - each row is one
# poll, not a cumulative counter), one series per wait_type.
# Upstream ref: GetWaitingTaskTrendAsync (ViewerDataService.BlockingTrends.cs)
_CURRENT_WAITS_DURATION_SQL = f"""
SELECT
    wt.collection_time AS time,
    wt.wait_type AS metric,
    SUM(wt.wait_duration_ms) AS value
FROM {collector('waiting_tasks')} AS wt
WHERE $__timeFilter(wt.collection_time)
  AND {server_filter('wt.server_id')}
  AND wt.wait_type IS NOT NULL
GROUP BY 1, 2
ORDER BY 1
"""

# Upstream ref: GetBlockedSessionTrendAsync (ViewerDataService.BlockingTrends.cs)
_CURRENT_WAITS_BLOCKED_SQL = f"""
SELECT
    wt.collection_time AS time,
    wt.database_name AS metric,
    COUNT(*) AS value
FROM {collector('waiting_tasks')} AS wt
WHERE $__timeFilter(wt.collection_time)
  AND {server_filter('wt.server_id')}
  AND wt.blocking_session_id > 0
GROUP BY 1, 2
ORDER BY 1
"""

# Keep every XE row; append a DMV row only for a blocked/blocker SPID pair that no XE row
# already covers within the same minute - upstream's BlockedProcessReportMerge. The two
# sources have different column shapes (DMV has no isolation/status/tran/priority/XML
# fields), so the DMV branch fills those with NULL to align the projection.
# Upstream ref: GetRecentBlockedProcessReportsAsync (ViewerDataService.Blocking.cs)
_BLOCKED_PROCESS_REPORTS_SQL = f"""
WITH xe AS (
    SELECT
        bpr.event_time, 'XE' AS source, bpr.database_name,
        bpr.blocked_spid, bpr.blocking_spid, bpr.wait_time_ms, bpr.wait_resource,
        bpr.lock_mode, bpr.blocked_status, bpr.blocking_status,
        bpr.blocked_isolation_level, bpr.blocking_isolation_level,
        bpr.blocked_transaction_name, bpr.blocked_priority,
        bpr.blocked_transaction_count, bpr.blocked_log_used,
        bpr.blocked_login_name, bpr.blocked_host_name, bpr.blocked_client_app,
        bpr.blocked_sql_text, bpr.blocking_login_name, bpr.blocking_host_name,
        bpr.blocking_client_app, bpr.blocking_sql_text
    FROM {collector('blocked_process_reports')} AS bpr
    WHERE $__timeFilter(bpr.collection_time)
      AND {server_filter('bpr.server_id')}
      AND {multi_filter('bpr.database_name', 'database')}
),
dmv AS (
    SELECT
        d.event_time, 'DMV' AS source, d.database_name,
        d.blocked_spid, d.blocking_spid, d.wait_time_ms,
        NULL::text AS wait_resource, d.lock_mode,
        NULL::text AS blocked_status, d.blocking_status,
        NULL::text AS blocked_isolation_level, NULL::text AS blocking_isolation_level,
        NULL::text AS blocked_transaction_name, NULL::integer AS blocked_priority,
        NULL::integer AS blocked_transaction_count, NULL::bigint AS blocked_log_used,
        d.blocked_login_name, d.blocked_host_name, d.blocked_client_app,
        d.blocked_sql_text, d.blocking_login_name, d.blocking_host_name,
        d.blocking_client_app, d.blocking_sql_text
    FROM {collector('dmv_blocking_snapshots')} AS d
    WHERE $__timeFilter(d.collection_time)
      AND {server_filter('d.server_id')}
      AND {multi_filter('d.database_name', 'database')}
      AND NOT EXISTS (
          SELECT 1 FROM xe
          WHERE xe.blocked_spid = d.blocked_spid
            AND xe.blocking_spid = d.blocking_spid
            AND date_trunc('minute', xe.event_time) = date_trunc('minute', d.event_time)
      )
)
SELECT
    r.event_time AS "Event Time",
    r.source AS "Source",
    r.database_name AS "Database",
    r.blocked_spid AS "Blocked SPID",
    r.blocking_spid AS "Blocking SPID",
    r.wait_time_ms AS "Wait (ms)",
    r.wait_resource AS "Wait Resource",
    r.lock_mode AS "Lock Mode",
    r.blocked_status AS "Blocked Status",
    r.blocking_status AS "Blocking Status",
    r.blocked_isolation_level AS "Blocked Isolation",
    r.blocking_isolation_level AS "Blocking Isolation",
    r.blocked_transaction_name AS "Blocked Tran",
    r.blocked_priority AS "Blocked Priority",
    r.blocked_transaction_count AS "Blocked Tran Count",
    r.blocked_log_used AS "Blocked Log Used",
    r.blocked_login_name AS "Blocked Login",
    r.blocked_host_name AS "Blocked Host",
    r.blocked_client_app AS "Blocked App",
    r.blocked_sql_text AS "Blocked SQL",
    r.blocking_login_name AS "Blocking Login",
    r.blocking_host_name AS "Blocking Host",
    r.blocking_client_app AS "Blocking App",
    r.blocking_sql_text AS "Blocking SQL"
FROM (SELECT * FROM xe UNION ALL SELECT * FROM dmv) AS r
ORDER BY r.event_time DESC
LIMIT 200
"""

_DATABASE_VAR_SQL = f"""
SELECT database_name FROM (
    SELECT bpr.database_name
    FROM {collector('blocked_process_reports')} AS bpr
    WHERE $__timeFilter(bpr.collection_time) AND {server_filter('bpr.server_id')}
    UNION
    SELECT d.database_name
    FROM {collector('dmv_blocking_snapshots')} AS d
    WHERE $__timeFilter(d.collection_time) AND {server_filter('d.server_id')}
) AS dbs
WHERE database_name IS NOT NULL
ORDER BY 1
"""


# Parses per-process attributes from the deadlock graph XML; "Object(s)" is correlated
# separately since resource-list entries are siblings of process-list, not children.
# Per-lock owner/waiter mode is not ported; the rendered graph is covered by
# deadlock_detail()'s raw-XML export instead.
# Upstream ref: GetRecentDeadlocksAsync (ViewerDataService.Deadlock.cs)
def _deadlock_participants_sql(graphs_filter: str, row_filter: str, limit: int) -> str:
    """Shared by the Deadlocks grid and deadlock_detail(); graphs_filter scopes which
    deadlock(s) to read, row_filter adds a predicate on the final rows."""
    return f"""
WITH graphs AS (
    SELECT
        dl.server_id, dl.deadlock_id, dl.deadlock_time, dl.victim_process_id,
        dl.deadlock_graph_xml::xml AS graph
    FROM {collector('deadlocks')} AS dl
    WHERE {graphs_filter}
),
processes AS (
    SELECT
        g.server_id,
        g.deadlock_id,
        g.deadlock_time,
        g.victim_process_id,
        (xpath('/*/@id', proc))[1]::text AS process_id,
        (xpath('/*/@spid', proc))[1]::text AS spid,
        (xpath('/*/@waittime', proc))[1]::text::bigint AS wait_ms,
        (xpath('/*/@lockMode', proc))[1]::text AS lock_mode,
        (xpath('/*/@waitresource', proc))[1]::text AS wait_resource,
        (xpath('/*/@isolationlevel', proc))[1]::text AS isolation_level,
        (xpath('/*/@currentdbname', proc))[1]::text AS database_name,
        (xpath('/*/@trancount', proc))[1]::text::integer AS tran_count,
        (xpath('/*/@loginname', proc))[1]::text AS login_name,
        (xpath('/*/@hostname', proc))[1]::text AS host_name,
        (xpath('/*/@clientapp', proc))[1]::text AS client_app,
        (xpath('/*/inputbuf/text()', proc))[1]::text AS sql_text
    FROM graphs g
    CROSS JOIN LATERAL unnest(xpath('/deadlock/process-list/process', g.graph)) AS proc
),
lock_elems AS (
    SELECT g.deadlock_id, lock
    FROM graphs g
    CROSS JOIN LATERAL unnest(xpath('/deadlock/resource-list/*', g.graph)) AS lock
),
participants AS (
    SELECT
        le.deadlock_id,
        (xpath('/*/@objectname', le.lock))[1]::text AS object_name,
        xpath('/*/owner-list/owner/@id', le.lock) AS owner_ids,
        xpath('/*/waiter-list/waiter/@id', le.lock) AS waiter_ids
    FROM lock_elems le
),
object_by_process AS (
    SELECT p.deadlock_id, unnest(p.owner_ids)::text AS process_id, p.object_name
    FROM participants p
    UNION ALL
    SELECT p.deadlock_id, unnest(p.waiter_ids)::text AS process_id, p.object_name
    FROM participants p
),
object_agg AS (
    SELECT deadlock_id, process_id, string_agg(DISTINCT object_name, ', ') AS objects
    FROM object_by_process
    WHERE object_name IS NOT NULL
    GROUP BY deadlock_id, process_id
)
SELECT
    /* ::text: a numeric field round-trips through a JS double in the drill-down data
       link and loses precision past 2^53, which this id exceeds. */
    p.deadlock_id::text AS "deadlock_id",
    p.deadlock_time AS "Time",
    CASE WHEN p.process_id = p.victim_process_id THEN 'Victim' ELSE '' END AS "Victim",
    p.spid AS "SPID",
    p.database_name AS "Database",
    oa.objects AS "Object(s)",
    p.lock_mode AS "Lock Mode",
    p.wait_resource AS "Wait Resource",
    p.wait_ms AS "Wait (ms)",
    p.isolation_level AS "Isolation",
    p.tran_count AS "Tran Count",
    p.login_name AS "Login",
    p.host_name AS "Host",
    p.client_app AS "App",
    p.sql_text AS "SQL Text"
FROM processes p
{server_join('p.server_id')}
LEFT JOIN object_agg AS oa
  ON oa.deadlock_id = p.deadlock_id AND oa.process_id = p.process_id
WHERE {row_filter}
ORDER BY p.deadlock_time DESC, p.spid
LIMIT {limit}
"""


_DEADLOCKS_SQL = _deadlock_participants_sql(
    f"$__timeFilter(dl.collection_time) AND {server_filter('dl.server_id')}",
    multi_filter("p.database_name", "database"),
    200,
)

# deadlock_id is a globally-unique bigint, not scoped per server, so this looks it up
# without a server_filter. Sentinel-guarded so a cold arrival with no id yet doesn't error.
_DEADLOCK_ID_FILTER = (
    "(${deadlock_id:sqlstring} = '*' OR ${deadlock_id:sqlstring} = '' "
    "OR dl.deadlock_id::text = ${deadlock_id:sqlstring})"
)

_DEADLOCK_DETAIL_PARTICIPANTS_SQL = _deadlock_participants_sql(
    _DEADLOCK_ID_FILTER, "true", 200
)

# Raw graph XML, exportable - Grafana has no XDL renderer.
# Upstream ref: ViewerServerTab.Deadlock.cs opens the graph viewer off this column.
_DEADLOCK_GRAPH_SQL = f"""
SELECT
    dl.deadlock_time AS "Time",
    srv.name AS "Server",
    dl.database_name AS "Database",
    dl.victim_sql_text AS "Victim SQL",
    dl.deadlock_graph_xml AS "Deadlock Graph (XML)"
FROM {collector('deadlocks')} AS dl
{server_join('dl.server_id')}
WHERE {_DEADLOCK_ID_FILTER}
ORDER BY dl.deadlock_time DESC
LIMIT 1
"""


def blocking():
    """Build the Blocking & Deadlocks dashboard."""
    reset_id()
    panels: list[dict] = []

    y = subtab(
        panels,
        "Trends",
        0,
        [
            (
                8,
                9,
                lambda x, y, w, h: timeseries(
                    "Lock Wait Trend",
                    x,
                    y,
                    w,
                    h,
                    [target(_LOCK_WAIT_TREND_SQL)],
                    unit="ms",
                    axis_label="Lock Wait Time (ms/sec)",
                ),
            ),
            (
                8,
                9,
                lambda x, y, w, h: timeseries(
                    "Blocking Trend",
                    x,
                    y,
                    w,
                    h,
                    [target(_BLOCKING_TREND_SQL)],
                    bars=True,
                    axis_label="Blocking Events per Minute",
                ),
            ),
            (
                8,
                9,
                lambda x, y, w, h: timeseries(
                    "Deadlock Trend",
                    x,
                    y,
                    w,
                    h,
                    [target(_DEADLOCK_TREND_SQL)],
                    bars=True,
                    axis_label="Deadlocks per Minute",
                ),
            ),
        ],
    )

    y = subtab(
        panels,
        "Blocking Stats",
        y,
        [
            (
                12,
                8,
                lambda x, y, w, h: timeseries(
                    "Blocking Duration (Max/Avg)",
                    x,
                    y,
                    w,
                    h,
                    [target(_BLOCKING_DURATION_MAX_AVG_SQL)],
                    unit="ms",
                ),
            ),
            (
                12,
                8,
                lambda x, y, w, h: timeseries(
                    "Blocking Total Duration",
                    x,
                    y,
                    w,
                    h,
                    [target(_BLOCKING_TOTAL_DURATION_SQL)],
                    unit="ms",
                ),
            ),
            (
                12,
                8,
                lambda x, y, w, h: timeseries(
                    "Deadlock Wait (Max/Avg)",
                    x,
                    y,
                    w,
                    h,
                    [target(_DEADLOCK_WAIT_MAX_AVG_SQL)],
                    unit="ms",
                ),
            ),
            (
                12,
                8,
                lambda x, y, w, h: timeseries(
                    "Deadlock Total Wait",
                    x,
                    y,
                    w,
                    h,
                    [target(_DEADLOCK_TOTAL_WAIT_SQL)],
                    unit="ms",
                ),
            ),
            (12, 4, stat_grid(_BLOCKING_STATS_SUMMARY_DURATION, cols=4)),
            (12, 4, stat_grid(_BLOCKING_STATS_SUMMARY_DEADLOCK, cols=3)),
        ],
    )

    y = subtab(
        panels,
        "Current Waits",
        y,
        [
            (
                12,
                9,
                lambda x, y, w, h: timeseries(
                    "Current Waits Duration",
                    x,
                    y,
                    w,
                    h,
                    [target(_CURRENT_WAITS_DURATION_SQL)],
                    unit="ms",
                    axis_label="Wait Duration (ms)",
                ),
            ),
            (
                12,
                9,
                lambda x, y, w, h: timeseries(
                    "Current Waits Blocked (by Database)",
                    x,
                    y,
                    w,
                    h,
                    [target(_CURRENT_WAITS_BLOCKED_SQL)],
                    axis_label="Blocked Sessions",
                ),
            ),
        ],
    )

    y = subtab(
        panels,
        "Blocked Process Reports",
        y,
        [
            (
                24,
                14,
                lambda x, y, w, h: table(
                    "Blocked Process Reports",
                    x,
                    y,
                    w,
                    h,
                    _BLOCKED_PROCESS_REPORTS_SQL,
                    sort_by=[{"displayName": "Event Time", "desc": True}],
                    overrides=[
                        # IsLongBlock (ViewerBlockedProcessRow) - 30s+ waits tint red,
                        # matching the WPF grid's BlockingRowStyle DataTrigger.
                        col_thresholds("Wait (ms)", ("green", None), ("red", 30000)),
                    ],
                    description=(
                        "Merges the blocked-process-report trace (XE) with DMV blocking "
                        "snapshots, XE preferred; a DMV row only fills a blocked/blocker "
                        "pair no XE row already covers in the same minute."
                    ),
                ),
            ),
        ],
    )

    deadlock_drill = col_datalink(
        "Time",
        "View deadlock detail",
        "/d/darling-deadlock-detail?${__url_time_range}"
        "&var-deadlock_id=${__data.fields.deadlock_id}",
    )

    subtab(
        panels,
        "Deadlocks",
        y,
        [
            (
                24,
                14,
                lambda x, y, w, h: table(
                    "Deadlocks",
                    x,
                    y,
                    w,
                    h,
                    _DEADLOCKS_SQL,
                    sort_by=[{"displayName": "Time", "desc": True}],
                    overrides=[col_hidden("deadlock_id"), deadlock_drill],
                    description=(
                        "One row per process in each deadlock graph, parsed from "
                        "deadlock_graph_xml. Click a Time value to open the full "
                        "participant detail and exportable graph XML. Per-lock "
                        "owner/waiter role is not portable - see the module docstring."
                    ),
                ),
            ),
        ],
    )

    database_var = query_var(
        "database",
        "Database",
        _DATABASE_VAR_SQL,
        "Databases seen in blocking/deadlock events over the window.",
    )

    return dashboard(
        uid("blocking-deadlocks"), "Blocking & Deadlocks", panels, [server_var(), database_var]
    )


def deadlock_detail():
    """Deadlock drill-down: full participant grid plus exportable graph XML.

    Reached from the Blocking dashboard's Deadlocks grid data link.
    """
    reset_id()
    panels: list[dict] = []

    y = subtab(
        panels,
        "Participants",
        0,
        [
            (
                24,
                12,
                lambda x, y, w, h: table(
                    "Deadlock Participants",
                    x,
                    y,
                    w,
                    h,
                    _DEADLOCK_DETAIL_PARTICIPANTS_SQL,
                    sort_by=[{"displayName": "Time", "desc": True}],
                    overrides=[col_hidden("deadlock_id")],
                    description=(
                        "One row per process in this deadlock's graph, parsed from "
                        "deadlock_graph_xml."
                    ),
                ),
            ),
        ],
    )

    subtab(
        panels,
        "Deadlock Graph (XML)",
        y,
        [
            (
                24,
                10,
                lambda x, y, w, h: table(
                    "Deadlock Graph (XML)",
                    x,
                    y,
                    w,
                    h,
                    _DEADLOCK_GRAPH_SQL,
                    description=(
                        "Full deadlock_graph_xml for this event. Grafana has no XDL "
                        "graph renderer. To export: panel menu (three dots) -> Inspect "
                        "-> Data -> Download CSV; the Deadlock Graph (XML) column holds "
                        "the complete XML."
                    ),
                ),
            ),
        ],
    )

    return detail_dashboard(
        uid("deadlock-detail"),
        "Deadlock Detail",
        panels,
        [server_var(), text_var("deadlock_id", "Deadlock ID", "*")],
        time_from="now-24h",
    )
