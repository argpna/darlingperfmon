"""Configuration dashboard (Darling line).

Upstream ref: ViewerDataService.Config.cs (ServerConfigSql, DatabaseConfigSql,
DatabaseScopedConfigSql, TraceFlagsSql).

Every read is the latest capture for the server, not a time range: the config collectors run
on connect, so `capture_time = MAX(capture_time)` is the current state. These panels
deliberately ignore the dashboard's time range, as upstream ignores its toolbar window.
"""

from ._shared import (
    collector,
    col_thresholds,
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

_DATABASE_VAR_SQL = f"""
SELECT DISTINCT dc.database_name
FROM {collector('database_config')} AS dc
WHERE {server_filter('dc.server_id')}
ORDER BY 1
"""


def configuration():
    """Build the Configuration dashboard."""
    reset_id()
    panels: list[dict] = []

    y = subtab(
        panels,
        "Server Configuration",
        0,
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

    subtab(
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

    return dashboard(
        uid("configuration"),
        "Configuration",
        panels,
        [
            server_var(),
            query_var(
                "database",
                "Database",
                _DATABASE_VAR_SQL,
                "Scopes the database and scoped-configuration grids.",
            ),
        ],
    )
