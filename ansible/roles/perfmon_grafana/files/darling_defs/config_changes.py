"""Configuration Changes dashboard (Darling line).

Upstream ref: ViewerDataService.ConfigChanges.cs, ConfigChangeDiff (PerformanceMonitor.Common).

The Darling store has no change views: config collectors append a FULL snapshot every capture,
and the change history is a diff of consecutive captures. Upstream diffs in C#; the same walk
is a LAG window function here, which is the same computation on the database side.

Windowing matches upstream exactly and is easy to get wrong: the snapshot read is bounded only
at the window END, and the range filter is applied to the computed change time. That keeps the
capture immediately BEFORE the window as the diff baseline, so a change landing on the first
in-window capture is still detected. Lower-bounding the snapshot read instead would drop that
baseline and under-report the left edge.

Change granularity is the service's connect cadence, since the config collectors run on
connect (frequency 0). A server that has only ever been connected to once holds a single
capture, and a diff needs two, so it shows no changes rather than a wrong one.

Two Dashboard-edition columns are omitted because Darling does not collect them, rather than
fabricated: server-config `description` and database-config `setting_type`. `requires_restart`
is NOT one of those - it is derivable from collected columns, so it is kept.
"""

from ._shared import (
    collector,
    dashboard,
    multi_filter,
    query_var,
    reset_id,
    server_filter,
    server_join,
    server_var,
    status_colors,
    subtab,
    table,
    uid,
)

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

# Upper-bounded at the window end only, so the pre-window capture survives as the baseline.
_UPTO_WINDOW_END = "capture_time <= $__timeTo()"

_SERVER_CHANGES_SQL = f"""
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
      AND sc.{_UPTO_WINDOW_END}
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
  AND $__timeFilter(w.capture_time)
ORDER BY w.capture_time DESC, w.configuration_name
"""


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


# The LAG has to happen on the wide row and the unpivot after it: a window function is not
# allowed inside a VALUES list.
_DATABASE_CHANGES_SQL = f"""
WITH lagged AS (
    SELECT
        dc.server_id,
        dc.capture_time,
        dc.database_name,
        LAG(dc.capture_time) OVER w AS prev_capture,
        {_db_lagged_columns()}
    FROM {collector('database_config')} AS dc
    WHERE {server_filter('dc.server_id')}
      AND dc.{_UPTO_WINDOW_END}
      AND {multi_filter('dc.database_name', 'database')}
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
  AND $__timeFilter(w.capture_time)
ORDER BY w.capture_time DESC, w.database_name, w.setting_name
"""

# A trace-flag row exists only while the flag is enabled, so this is a SET-diff of consecutive
# captures rather than a per-key value walk: appearing is `enabled`, vanishing is `disabled`,
# a status or scope move is `modified`. A vanished flag has no row to walk from, so the diff
# runs over a (capture x flag) grid with both captures outer-joined onto it - that is what
# makes an absence visible at all.
_TRACE_FLAG_CHANGES_SQL = f"""
WITH captures AS (
    SELECT DISTINCT tf.server_id, tf.capture_time
    FROM {collector('trace_flags')} AS tf
    WHERE {server_filter('tf.server_id')}
      AND tf.{_UPTO_WINDOW_END}
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
      AND tf.{_UPTO_WINDOW_END}
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
WHERE $__timeFilter(d.capture_time)
ORDER BY d.capture_time DESC, d.trace_flag
"""

_DATABASE_VAR_SQL = f"""
SELECT DISTINCT dc.database_name
FROM {collector('database_config')} AS dc
WHERE {server_filter('dc.server_id')}
ORDER BY 1
"""


def config_changes():
    """Build the Configuration Changes dashboard."""
    reset_id()
    panels: list[dict] = []

    y = subtab(
        panels,
        "Server Config Changes",
        0,
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

    subtab(
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

    return dashboard(
        uid("config-changes"),
        "Configuration Changes",
        panels,
        [
            server_var(),
            query_var(
                "database",
                "Database",
                _DATABASE_VAR_SQL,
                "Scopes the database configuration change history.",
            ),
        ],
        time_from="now-7d",
    )
