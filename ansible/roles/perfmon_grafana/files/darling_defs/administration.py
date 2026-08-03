"""Administration dashboard (Darling line) - merges Configuration, Configuration
Changes, and Running Jobs dashboards: current setup, recent changes, and scheduled jobs.

Upstream ref: ViewerDataService.Config.cs, ViewerDataService.ConfigChanges.cs,
ViewerServerTab.RunningJobs.cs.

Configuration's grids read each server's latest capture, not the dashboard time range.
Configuration and Configuration Changes each defined their own `database` variable; the
Configuration Changes one is renamed `change_database` here to avoid a name collision.
"""

from ._shared import (
    col_thresholds,
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
    stat_grid,
    status_colors,
    subtab,
    table,
    thresholds,
    uid,
    UTC_NOW,
)


def _latest(base: str, alias: str) -> str:
    """The newest capture per server, so a multi-server view shows each one's own snapshot."""
    return f"""{alias}.capture_time = (
        SELECT MAX(inner_cap.capture_time) FROM {collector(base)} AS inner_cap
        WHERE inner_cap.server_id = {alias}.server_id
    )"""


def _yes_no(col: str) -> str:
    """The grids' bool rendering: upstream shows Yes/No, never true/false."""
    return f"CASE WHEN {col} THEN 'Yes' ELSE 'No' END"


_SERVER_CONFIG_SQL = f"""
SELECT
    srv.name AS "Server",
    sc.configuration_name AS "Setting",
    sc.value_configured AS "Configured",
    sc.value_in_use AS "In Use",
    {_yes_no('sc.is_dynamic')} AS "Dynamic",
    {_yes_no('sc.is_advanced')} AS "Advanced",
    /* ValuesMatch on the row class: a mismatch is a setting awaiting a restart. */
    {_yes_no('sc.value_configured = sc.value_in_use')} AS "Values Match"
FROM {collector('server_config')} AS sc
{server_join('sc.server_id')}
WHERE {server_filter('sc.server_id')}
  AND {_latest('server_config', 'sc')}
ORDER BY srv.name, sc.configuration_name
"""

_DATABASE_CONFIG_SQL = f"""
SELECT
    srv.name AS "Server",
    dc.database_name AS "Database",
    dc.state_desc AS "State",
    dc.compatibility_level AS "Compat Level",
    dc.collation_name AS "Collation",
    dc.recovery_model AS "Recovery Model",
    {_yes_no('dc.is_read_only')} AS "Read Only",
    {_yes_no('dc.is_auto_close_on')} AS "Auto Close",
    {_yes_no('dc.is_auto_shrink_on')} AS "Auto Shrink",
    {_yes_no('dc.is_auto_create_stats_on')} AS "Auto Create Stats",
    {_yes_no('dc.is_auto_update_stats_on')} AS "Auto Update Stats",
    {_yes_no('dc.is_auto_update_stats_async_on')} AS "Auto Update Stats Async",
    {_yes_no('dc.is_read_committed_snapshot_on')} AS "RCSI",
    dc.snapshot_isolation_state AS "Snapshot Isolation",
    {_yes_no('dc.is_parameterization_forced')} AS "Forced Parameterization",
    {_yes_no('dc.is_query_store_on')} AS "Query Store",
    {_yes_no('dc.is_encrypted')} AS "Encrypted",
    {_yes_no('dc.is_trustworthy_on')} AS "Trustworthy",
    {_yes_no('dc.is_db_chaining_on')} AS "DB Chaining",
    {_yes_no('dc.is_broker_enabled')} AS "Broker",
    {_yes_no('dc.is_cdc_enabled')} AS "CDC",
    {_yes_no('dc.is_mixed_page_allocation_on')} AS "Mixed Page Allocation",
    dc.log_reuse_wait_desc AS "Log Reuse Wait",
    dc.page_verify_option AS "Page Verify",
    dc.target_recovery_time_seconds AS "Target Recovery (s)",
    dc.delayed_durability AS "Delayed Durability",
    {_yes_no('dc.is_accelerated_database_recovery_on')} AS "ADR",
    {_yes_no('dc.is_memory_optimized_enabled')} AS "Memory Optimized",
    {_yes_no('dc.is_optimized_locking_on')} AS "Optimized Locking"
FROM {collector('database_config')} AS dc
{server_join('dc.server_id')}
WHERE {server_filter('dc.server_id')}
  AND {_latest('database_config', 'dc')}
  AND {multi_filter('dc.database_name', 'database')}
ORDER BY srv.name, dc.database_name
"""

_SCOPED_CONFIG_SQL = f"""
SELECT
    srv.name AS "Server",
    dsc.database_name AS "Database",
    dsc.configuration_name AS "Setting",
    dsc.value AS "Value",
    dsc.value_for_secondary AS "Value For Secondary"
FROM {collector('database_scoped_config')} AS dsc
{server_join('dsc.server_id')}
WHERE {server_filter('dsc.server_id')}
  AND {_latest('database_scoped_config', 'dsc')}
  AND {multi_filter('dsc.database_name', 'database')}
ORDER BY srv.name, dsc.database_name, dsc.configuration_name
"""

# A row exists only while a flag is enabled, so this grid is the enabled set.
_TRACE_FLAGS_SQL = f"""
SELECT
    srv.name AS "Server",
    tf.trace_flag AS "Trace Flag",
    CASE WHEN tf.status THEN 'Enabled' ELSE 'Disabled' END AS "Status",
    {_yes_no('tf.is_global')} AS "Global",
    {_yes_no('tf.is_session')} AS "Session"
FROM {collector('trace_flags')} AS tf
{server_join('tf.server_id')}
WHERE {server_filter('tf.server_id')}
  AND {_latest('trace_flags', 'tf')}
ORDER BY srv.name, tf.trace_flag
"""

_CONFIG_DATABASE_VAR_SQL = f"""
SELECT DISTINCT dc.database_name
FROM {collector('database_config')} AS dc
WHERE {server_filter('dc.server_id')}
ORDER BY 1
"""

# The 27 setting columns the wide sys.databases snapshot carries, unpivoted one row per
# changed setting so setting_name is the literal column name. Upstream ref:
# ConfigChangeDiff.DatabaseConfigChangeSettingNames.
_DB_SETTINGS = (
    "state_desc",
    "compatibility_level",
    "collation_name",
    "recovery_model",
    "is_read_only",
    "is_auto_close_on",
    "is_auto_shrink_on",
    "is_auto_create_stats_on",
    "is_auto_update_stats_on",
    "is_auto_update_stats_async_on",
    "is_read_committed_snapshot_on",
    "snapshot_isolation_state",
    "is_parameterization_forced",
    "is_query_store_on",
    "is_encrypted",
    "is_trustworthy_on",
    "is_db_chaining_on",
    "is_broker_enabled",
    "is_cdc_enabled",
    "is_mixed_page_allocation_on",
    "log_reuse_wait_desc",
    "page_verify_option",
    "target_recovery_time_seconds",
    "delayed_durability",
    "is_accelerated_database_recovery_on",
    "is_memory_optimized_enabled",
    "is_optimized_locking_on",
)


def _db_lagged_columns() -> str:
    """Each setting as text alongside its previous capture's value."""
    return ",\n        ".join(
        f"dc.{name}::text AS {name},"
        f"\n        LAG(dc.{name}::text) OVER w AS prev_{name}"
        for name in _DB_SETTINGS
    )


def _db_unpivot() -> str:
    """The lagged wide row folded to one row per setting, for the value comparison."""
    return ",\n        ".join(
        f"('{name}', l.{name}, l.prev_{name})" for name in _DB_SETTINGS
    )


# window_end/since are parameterized so the same diff logic serves both the change-history
# tables (bounded by the dashboard's own time range) and the stat row's fixed 24h count
# (independent of it, like Wait Analysis's and Collection Health's "right now" tiles). since
# is a column-name -> predicate function (_dashboard_time_filter / _last_24h_filter below).
def _server_changes_sql(window_end: str, since) -> str:
    return f"""
WITH walked AS (
    SELECT
        sc.server_id,
        sc.capture_time,
        sc.configuration_name,
        sc.value_configured,
        sc.value_in_use,
        sc.is_dynamic,
        sc.is_advanced,
        LAG(sc.value_configured) OVER w AS prev_configured,
        LAG(sc.value_in_use) OVER w AS prev_in_use,
        LAG(sc.capture_time) OVER w AS prev_capture
    FROM {collector('server_config')} AS sc
    WHERE {server_filter('sc.server_id')}
      AND sc.capture_time <= {window_end}
    WINDOW w AS (
        PARTITION BY sc.server_id, sc.configuration_name ORDER BY sc.capture_time
    )
)
SELECT
    w.capture_time AS "Change Time",
    srv.name AS "Server",
    w.configuration_name AS "Setting",
    w.prev_configured AS "Old Configured",
    w.value_configured AS "New Configured",
    w.prev_in_use AS "Old In Use",
    w.value_in_use AS "New In Use",
    CASE WHEN w.is_dynamic THEN 'Yes' ELSE 'No' END AS "Dynamic",
    CASE WHEN w.is_advanced THEN 'Yes' ELSE 'No' END AS "Advanced",
    /* The Dashboard view's own definition: a non-dynamic setting whose configured value
       differs from the in-use value is waiting on a restart. */
    CASE WHEN w.is_dynamic = FALSE AND w.value_configured IS DISTINCT FROM w.value_in_use
         THEN 'Yes' ELSE 'No' END AS "Requires Restart",
    CASE
        WHEN w.prev_configured IS DISTINCT FROM w.value_configured
            THEN 'Configured value changed from ' || COALESCE(w.prev_configured::text, '')
                 || ' to ' || COALESCE(w.value_configured::text, '')
        WHEN w.prev_in_use IS DISTINCT FROM w.value_in_use
            THEN 'In-use value changed from ' || COALESCE(w.prev_in_use::text, '')
                 || ' to ' || COALESCE(w.value_in_use::text, '')
        ELSE 'Value unchanged'
    END AS "Change"
FROM walked AS w
{server_join('w.server_id')}
WHERE w.prev_capture IS NOT NULL
  AND (w.prev_configured IS DISTINCT FROM w.value_configured
       OR w.prev_in_use IS DISTINCT FROM w.value_in_use)
  AND {since('w.capture_time')}
ORDER BY w.capture_time DESC, w.configuration_name
"""


def _database_changes_sql(window_end: str, since) -> str:
    return f"""
WITH lagged AS (
    SELECT
        dc.server_id,
        dc.capture_time,
        dc.database_name,
        LAG(dc.capture_time) OVER w AS prev_capture,
        {_db_lagged_columns()}
    FROM {collector('database_config')} AS dc
    WHERE {server_filter('dc.server_id')}
      AND dc.capture_time <= {window_end}
      AND {multi_filter('dc.database_name', 'change_database')}
    WINDOW w AS (PARTITION BY dc.server_id, dc.database_name ORDER BY dc.capture_time)
),
walked AS (
    SELECT
        l.server_id,
        l.capture_time,
        l.database_name,
        l.prev_capture,
        s.setting_name,
        s.new_value,
        s.old_value
    FROM lagged AS l
    CROSS JOIN LATERAL (VALUES
        {_db_unpivot()}
    ) AS s(setting_name, new_value, old_value)
)
SELECT
    w.capture_time AS "Change Time",
    srv.name AS "Server",
    w.database_name AS "Database",
    w.setting_name AS "Setting",
    w.old_value AS "Old Value",
    w.new_value AS "New Value",
    CASE
        WHEN w.old_value IS NULL AND w.new_value IS NOT NULL THEN 'Set to: ' || w.new_value
        WHEN w.old_value IS NOT NULL AND w.new_value IS NULL
            THEN 'Cleared (was: ' || w.old_value || ')'
        ELSE 'Changed from ' || COALESCE(w.old_value, '') || ' to '
             || COALESCE(w.new_value, '')
    END AS "Change"
FROM walked AS w
{server_join('w.server_id')}
WHERE w.prev_capture IS NOT NULL
  AND w.old_value IS DISTINCT FROM w.new_value
  AND {since('w.capture_time')}
ORDER BY w.capture_time DESC, w.database_name, w.setting_name
"""


# A trace-flag row exists only while the flag is enabled, so this is a SET-diff of consecutive
# captures rather than a per-key value walk: appearing is `enabled`, vanishing is `disabled`,
# a status or scope move is `modified`. A vanished flag has no row to walk from, so the diff
# runs over a (capture x flag) grid with both captures outer-joined onto it - that is what
# makes an absence visible at all.
def _trace_flag_changes_sql(window_end: str, since) -> str:
    return f"""
WITH captures AS (
    SELECT DISTINCT tf.server_id, tf.capture_time
    FROM {collector('trace_flags')} AS tf
    WHERE {server_filter('tf.server_id')}
      AND tf.capture_time <= {window_end}
),
paired AS (
    SELECT server_id, capture_time,
           LAG(capture_time) OVER (PARTITION BY server_id ORDER BY capture_time)
               AS prev_capture
    FROM captures
),
flags AS (
    SELECT DISTINCT tf.server_id, tf.trace_flag
    FROM {collector('trace_flags')} AS tf
    WHERE {server_filter('tf.server_id')}
      AND tf.capture_time <= {window_end}
),
diffed AS (
    SELECT
        p.server_id,
        p.capture_time,
        f.trace_flag,
        prv.status AS prev_status,
        cur.status AS new_status,
        COALESCE(cur.is_global, prv.is_global) AS is_global,
        COALESCE(cur.is_session, prv.is_session) AS is_session,
        CASE
            WHEN prv.trace_flag IS NULL THEN 'enabled'
            WHEN cur.trace_flag IS NULL THEN 'disabled'
            ELSE 'modified'
        END AS change_type
    FROM paired AS p
    JOIN flags AS f ON f.server_id = p.server_id
    LEFT JOIN {collector('trace_flags')} AS cur
        ON cur.server_id = p.server_id
       AND cur.capture_time = p.capture_time
       AND cur.trace_flag = f.trace_flag
    LEFT JOIN {collector('trace_flags')} AS prv
        ON prv.server_id = p.server_id
       AND prv.capture_time = p.prev_capture
       AND prv.trace_flag = f.trace_flag
    WHERE p.prev_capture IS NOT NULL
      /* Present in neither capture is not a change, it is a flag that exists elsewhere in
         the window; the two set-membership arms below are the enable/disable cases. */
      AND (cur.trace_flag IS NOT NULL OR prv.trace_flag IS NOT NULL)
      AND (prv.trace_flag IS NULL
           OR cur.trace_flag IS NULL
           OR prv.status IS DISTINCT FROM cur.status
           OR prv.is_global IS DISTINCT FROM cur.is_global
           OR prv.is_session IS DISTINCT FROM cur.is_session)
)
SELECT
    d.capture_time AS "Change Time",
    srv.name AS "Server",
    d.trace_flag AS "Trace Flag",
    d.change_type AS "Change Type",
    CASE WHEN d.prev_status IS NULL THEN '' WHEN d.prev_status THEN 'ON' ELSE 'OFF' END
        AS "Previous Status",
    CASE WHEN d.new_status IS NULL THEN '' WHEN d.new_status THEN 'ON' ELSE 'OFF' END
        AS "New Status",
    CASE WHEN d.is_global THEN 'GLOBAL' WHEN d.is_session THEN 'SESSION' ELSE 'UNKNOWN' END
        AS "Scope",
    CASE d.change_type
        WHEN 'enabled' THEN 'Trace flag ' || d.trace_flag || ' ENABLED'
        WHEN 'disabled' THEN 'Trace flag ' || d.trace_flag || ' DISABLED'
        WHEN 'modified' THEN 'Trace flag ' || d.trace_flag || ' scope changed'
        ELSE 'Status unchanged'
    END AS "Change"
FROM diffed AS d
{server_join('d.server_id')}
WHERE {since('d.capture_time')}
ORDER BY d.capture_time DESC, d.trace_flag
"""


def _dashboard_time_filter(col: str) -> str:
    return f"$__timeFilter({col})"


def _last_24h_filter(col: str) -> str:
    return f"{col} >= {UTC_NOW} - INTERVAL '24 hours'"


_SERVER_CHANGES_SQL = _server_changes_sql("$__timeTo()", _dashboard_time_filter)
_DATABASE_CHANGES_SQL = _database_changes_sql("$__timeTo()", _dashboard_time_filter)
_TRACE_FLAG_CHANGES_SQL = _trace_flag_changes_sql("$__timeTo()", _dashboard_time_filter)

_CHANGE_DATABASE_VAR_SQL = f"""
SELECT DISTINCT dc.database_name
FROM {collector('database_config')} AS dc
WHERE {server_filter('dc.server_id')}
ORDER BY 1
"""

# Upstream ref: ShouldShowMsdbBanner - PERMISSIONS status only, not any-error.
_MSDB_STATUS_SQL = f"""
SELECT DISTINCT ON (cl.server_id)
    srv.name AS "Server",
    cl.status AS "Status"
FROM {collector('collection_log')} AS cl
{server_join('cl.server_id')}
WHERE cl.collector_name = 'running_jobs'
  AND {server_filter('cl.server_id')}
ORDER BY cl.server_id, cl.collection_time DESC
"""

# Upstream ref: RunningJobsSql, extended to a per-server latest snapshot for multi-select $server.
_RUNNING_JOBS_SQL = f"""
WITH latest AS (
    SELECT
        rj.server_id,
        srv.name AS server_label,
        MAX(rj.collection_time) AS mx
    FROM {collector('running_jobs')} AS rj
    {server_join('rj.server_id')}
    WHERE $__timeFilter(rj.collection_time)
      AND {server_filter('rj.server_id')}
    GROUP BY rj.server_id, srv.name
)
SELECT
    l.server_label AS "Server",
    rj.job_name AS "Job Name",
    rj.start_time AS "Start Time",
    rj.current_duration_seconds AS "Current Duration",
    rj.avg_duration_seconds AS "Avg Duration",
    rj.p95_duration_seconds AS "P95 Duration",
    rj.percent_of_average AS "% of Average",
    CASE WHEN rj.is_running_long THEN 'Yes' ELSE '' END AS "Running Long",
    rj.successful_run_count AS "Successful Runs (30d)"
FROM latest AS l
JOIN {collector('running_jobs')} AS rj
  ON rj.server_id = l.server_id
 AND rj.collection_time = l.mx
ORDER BY rj.current_duration_seconds DESC
"""

# Stat row: non-default config count, config changes in the last 24h, currently running jobs.
# A short trailing window, not $__timeFilter - "right now" snapshot tiles, matching Wait
# Analysis's and Collection Health's stat rows. Darling collects configured vs in-use values,
# not SQL Server's shipped defaults, so "non-default" is approximated as configured-but-not-
# yet-applied drift (the same condition the Server Configuration grid's "Values Match" column
# already flags) rather than a true default-value comparison.
_PENDING_RESTART_SQL = f"""
SELECT COUNT(*) AS v
FROM {collector('server_config')} AS sc
WHERE {server_filter('sc.server_id')}
  AND {_latest('server_config', 'sc')}
  AND sc.value_configured IS DISTINCT FROM sc.value_in_use
"""

_CHANGES_24H_SQL = f"""
SELECT
    (SELECT COUNT(*) FROM ({_server_changes_sql(UTC_NOW, _last_24h_filter)}) s)
  + (SELECT COUNT(*) FROM ({_database_changes_sql(UTC_NOW, _last_24h_filter)}) s)
  + (SELECT COUNT(*) FROM ({_trace_flag_changes_sql(UTC_NOW, _last_24h_filter)}) s)
    AS v
"""

_RUNNING_JOBS_COUNT_SQL = f"""
WITH latest AS (
    SELECT rj.server_id, MAX(rj.collection_time) AS mx
    FROM {collector('running_jobs')} AS rj
    WHERE $__timeFilter(rj.collection_time)
      AND {server_filter('rj.server_id')}
    GROUP BY rj.server_id
)
SELECT COUNT(*) AS v
FROM latest AS l
JOIN {collector('running_jobs')} AS rj
  ON rj.server_id = l.server_id
 AND rj.collection_time = l.mx
"""

_STAT_ROW = [
    {
        "title": "Pending-Restart Settings",
        "sql": _PENDING_RESTART_SQL,
        "th": thresholds(("green", None), ("yellow", 1)),
    },
    {
        "title": "Config Changes (24h)",
        "sql": _CHANGES_24H_SQL,
        "th": thresholds(("green", None), ("yellow", 1)),
    },
    {
        "title": "Currently Running Jobs",
        "sql": _RUNNING_JOBS_COUNT_SQL,
        "th": thresholds(("text", None)),
    },
]


def administration():
    """Build the Administration dashboard."""
    reset_id()
    panels: list[dict] = []

    y = flow(panels, 0, [(24, 4, stat_grid(_STAT_ROW, cols=3))])

    y = subtab(
        panels,
        "Server Configuration",
        y,
        [
            (
                24,
                12,
                lambda x, y, w, h: table(
                    "sys.configurations",
                    x,
                    y,
                    w,
                    h,
                    _SERVER_CONFIG_SQL,
                    overrides=[
                        status_colors("Values Match", {"Yes": "text", "No": "orange"})
                    ],
                    description="Latest capture per server, not the dashboard time range.",
                ),
            )
        ],
    )

    y = subtab(
        panels,
        "Database Configuration",
        y,
        [
            (
                24,
                12,
                lambda x, y, w, h: table(
                    "sys.databases",
                    x,
                    y,
                    w,
                    h,
                    _DATABASE_CONFIG_SQL,
                    overrides=[
                        status_colors("Auto Shrink", {"Yes": "red", "No": "text"}),
                        status_colors("Auto Close", {"Yes": "red", "No": "text"}),
                        status_colors("Trustworthy", {"Yes": "orange", "No": "text"}),
                        col_thresholds("Compat Level", ("orange", None), ("text", 150)),
                    ],
                ),
            )
        ],
    )

    y = subtab(
        panels,
        "Scoped Configuration",
        y,
        [
            (
                24,
                12,
                lambda x, y, w, h: table(
                    "Database Scoped Configuration",
                    x,
                    y,
                    w,
                    h,
                    _SCOPED_CONFIG_SQL,
                ),
            )
        ],
    )

    y = subtab(
        panels,
        "Trace Flags",
        y,
        [
            (
                24,
                10,
                lambda x, y, w, h: table(
                    "Trace Flags",
                    x,
                    y,
                    w,
                    h,
                    _TRACE_FLAGS_SQL,
                    overrides=[
                        status_colors(
                            "Status", {"Enabled": "green", "Disabled": "text"}
                        )
                    ],
                ),
            )
        ],
    )

    y = subtab(
        panels,
        "Server Config Changes",
        y,
        [
            (
                24,
                11,
                lambda x, y, w, h: table(
                    "Server Configuration Changes",
                    x,
                    y,
                    w,
                    h,
                    _SERVER_CHANGES_SQL,
                    overrides=[
                        status_colors(
                            "Requires Restart", {"Yes": "orange", "No": "text"}
                        )
                    ],
                    sort_by=[{"displayName": "Change Time", "desc": True}],
                ),
            )
        ],
    )

    y = subtab(
        panels,
        "Database Config Changes",
        y,
        [
            (
                24,
                11,
                lambda x, y, w, h: table(
                    "Database Configuration Changes",
                    x,
                    y,
                    w,
                    h,
                    _DATABASE_CHANGES_SQL,
                    sort_by=[{"displayName": "Change Time", "desc": True}],
                ),
            )
        ],
    )

    y = subtab(
        panels,
        "Trace Flag Changes",
        y,
        [
            (
                24,
                11,
                lambda x, y, w, h: table(
                    "Trace Flag Changes",
                    x,
                    y,
                    w,
                    h,
                    _TRACE_FLAG_CHANGES_SQL,
                    overrides=[
                        status_colors(
                            "Change Type",
                            {
                                "enabled": "green",
                                "disabled": "red",
                                "modified": "yellow",
                            },
                        )
                    ],
                    sort_by=[{"displayName": "Change Time", "desc": True}],
                ),
            )
        ],
    )

    y = subtab(
        panels,
        "Running Jobs",
        y,
        [
            (
                24,
                5,
                lambda x, y, w, h: table(
                    "Msdb Access Status",
                    x,
                    y,
                    w,
                    h,
                    _MSDB_STATUS_SQL,
                    overrides=[
                        status_colors(
                            "Status",
                            {
                                "SUCCESS": "green",
                                "PERMISSIONS": "purple",
                                "ERROR": "red",
                                "YIELDED": "yellow",
                                "SKIPPED": "text",
                            },
                        ),
                    ],
                    description="PERMISSIONS means the login lacks msdb access.",
                ),
            ),
            (
                24,
                13,
                lambda x, y, w, h: table(
                    "Currently Running SQL Agent Jobs",
                    x,
                    y,
                    w,
                    h,
                    _RUNNING_JOBS_SQL,
                    sort_by=[{"displayName": "Current Duration", "desc": True}],
                    overrides=[
                        status_colors("Running Long", {"Yes": "red"}, cell_type="color-text"),
                        col_unit("Current Duration", "s"),
                        col_unit("Avg Duration", "s"),
                        col_unit("P95 Duration", "s"),
                        col_unit("% of Average", "percent"),
                    ],
                ),
            ),
        ],
    )

    return dashboard(
        uid("administration"),
        "Administration",
        panels,
        [
            server_var(),
            query_var(
                "database",
                "Database",
                _CONFIG_DATABASE_VAR_SQL,
                "Scopes the database and scoped-configuration grids.",
            ),
            query_var(
                "change_database",
                "Change Database",
                _CHANGE_DATABASE_VAR_SQL,
                "Scopes the database configuration change history.",
            ),
        ],
        time_from="now-7d",
    )
