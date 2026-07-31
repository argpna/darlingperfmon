"""Availability Groups dashboards (Darling line).

Upstream ref: AvailabilityGroupsTab.xaml(.cs), ViewerDataService.AvailabilityGroups.cs,
AgTopology (PerformanceMonitor.Common) for the banding and drain-minutes math ported below.

Fleet-wide by design, like upstream: an AG's primary and secondaries can sit on different
monitored servers, so narrowing to one server would show only its own half of the topology.
$server stays available to narrow the view; "All" (its default) matches upstream's scope.

Upstream renders one card per (reporting server, AG) with nested replica chips and a
database grid. Grafana has no card/nested-grid primitive, and a fleet running many AGs would
make a flat one-row-per-reporter table unwieldy, so this ports as two dashboards instead of
one: availability_groups() rolls up to one row per AG (worst severity, primary, counts) and
availability_group_detail() carries the full per-reporter replica/database tables, reached via
a data link scoped to the clicked AG. Severity is recomputed in SQL from AgTopology's banding
rules rather than read from a stored verdict, since none is stored - only the underlying DMV
state columns are.
"""

from ._shared import (
    HEALTH_STATUS_COLORS,
    col_datalink,
    col_unit,
    collector,
    dashboard,
    detail_dashboard,
    reset_id,
    server_filter,
    server_join,
    server_var,
    stat,
    status_colors,
    subtab,
    table,
    text_panel,
    text_var,
    thresholds,
    uid,
)


def _tri(col: str, yes: str = "Yes", no: str = "No", unknown: str = "—") -> str:
    """Tri-state boolean rendering: SQL Server's nullable bit columns are genuinely
    Yes/No/Unknown here, not just Yes/No."""
    return f"CASE WHEN {col} THEN '{yes}' WHEN {col} = false THEN '{no}' ELSE '{unknown}' END"


def _severity_label(int_expr: str) -> str:
    return f"""CASE {int_expr}
        WHEN 3 THEN 'Critical' WHEN 2 THEN 'Warning' WHEN 1 THEN 'Healthy' ELSE 'Unknown' END"""


# Upstream ref: AgTopology.ToReplica - worst of the five per-replica bands.
def _replica_severity_int(t: str) -> str:
    return f"""GREATEST(
        CASE UPPER({t}.synchronization_health_desc)
            WHEN 'HEALTHY' THEN 1 WHEN 'PARTIALLY_HEALTHY' THEN 2
            WHEN 'NOT_HEALTHY' THEN 3 ELSE 0 END,
        CASE UPPER({t}.connected_state_desc)
            WHEN 'CONNECTED' THEN 1 WHEN 'DISCONNECTED' THEN 3 ELSE 0 END,
        CASE UPPER({t}.operational_state_desc)
            WHEN 'ONLINE' THEN 1 WHEN 'PENDING' THEN 2 WHEN 'PENDING_FAILOVER' THEN 2
            WHEN 'OFFLINE' THEN 3 WHEN 'FAILED' THEN 3 WHEN 'FAILED_NO_QUORUM' THEN 3
            ELSE 0 END,
        CASE UPPER({t}.recovery_health_desc)
            WHEN 'ONLINE' THEN 1 WHEN 'ONLINE_IN_PROGRESS' THEN 2 ELSE 0 END,
        CASE UPPER({t}.role_desc)
            WHEN 'PRIMARY' THEN 1 WHEN 'SECONDARY' THEN 1 WHEN 'RESOLVING' THEN 2 ELSE 0 END
    )"""


# Upstream ref: AgTopology.DatabaseSyncSeverity - suspension outranks the state text, and
# SYNCHRONIZING only bands Warning on a non-async replica (async never reaches SYNCHRONIZED).
def _db_sync_severity_int(t: str) -> str:
    return f"""CASE
        WHEN {t}.is_suspended THEN 3
        WHEN UPPER({t}.synchronization_state_desc) = 'SYNCHRONIZED' THEN 1
        WHEN UPPER({t}.synchronization_state_desc) = 'SYNCHRONIZING' THEN
            CASE WHEN UPPER({t}.availability_mode_desc) = 'ASYNCHRONOUS_COMMIT' THEN 1
                 ELSE 2 END
        WHEN UPPER({t}.synchronization_state_desc) = 'NOT SYNCHRONIZING' THEN 3
        WHEN UPPER({t}.synchronization_state_desc) IN ('REVERTING', 'INITIALIZING') THEN 2
        ELSE 0
    END"""


# Upstream ref: AgTopology.DrainMinutes - queue/rate are both instantaneous gauges, so an
# empty queue is 0 regardless of rate and a stalled (rate<=0) non-empty queue has no ETA.
def _drain_minutes(queue_col: str, rate_col: str) -> str:
    return f"""CASE
        WHEN {queue_col} IS NULL THEN NULL
        WHEN {queue_col} <= 0 THEN 0
        WHEN {rate_col} IS NULL OR {rate_col} <= 0 THEN NULL
        ELSE round(({queue_col}::numeric / {rate_col}) / 60.0, 2)
    END"""


def _ag_filter(col: str) -> str:
    """Optional AG-name filter for the detail dashboard: sentinel default, see _shared's
    text_var doc for why ${ag:sqlstring} needs both the '*' and '' arms guarded."""
    return f"(${{ag:sqlstring}} = '*' OR ${{ag:sqlstring}} = '' OR UPPER({col}) = UPPER(${{ag:sqlstring}}))"


# Upstream ref: AgReplicaStatesSql - newest replica-grain snapshot per reporting server.
_LATEST_REPLICA_CTE = f"""
    SELECT r.server_id, r.server_name, r.collection_time, r.ag_name, r.replica_server_name,
           r.role_desc, r.is_local, r.operational_state_desc, r.connected_state_desc,
           r.recovery_health_desc, r.synchronization_health_desc, r.availability_mode_desc,
           r.failover_mode_desc, r.endpoint_url
    FROM {collector('ag_replica_states')} AS r
    JOIN (
        SELECT server_id, MAX(collection_time) AS max_collection_time
        FROM {collector('ag_replica_states')}
        GROUP BY server_id
    ) AS latest
      ON r.server_id = latest.server_id
      AND r.collection_time = latest.max_collection_time
"""

# Upstream ref: AgDatabaseReplicaStatesSql - windowed independently, since the two
# collectors sweep on their own schedules and their newest instants need not coincide.
_LATEST_DATABASE_CTE = f"""
    SELECT d.server_id, d.server_name, d.collection_time, d.ag_name, d.database_name,
           d.replica_server_name, d.is_local, d.synchronization_state_desc,
           d.log_send_queue_size, d.redo_queue_size, d.log_send_rate, d.redo_rate,
           d.is_suspended, d.suspend_reason_desc, d.availability_mode_desc,
           d.secondary_lag_seconds
    FROM {collector('ag_database_replica_states')} AS d
    JOIN (
        SELECT server_id, MAX(collection_time) AS max_collection_time
        FROM {collector('ag_database_replica_states')}
        GROUP BY server_id
    ) AS latest
      ON d.server_id = latest.server_id
      AND d.collection_time = latest.max_collection_time
"""


def _count_sql(select_expr: str) -> str:
    return f"""
WITH latest AS ({_LATEST_REPLICA_CTE})
SELECT {select_expr}
FROM latest
{server_join('latest.server_id')}
WHERE {server_filter('latest.server_id')}
"""


# Upstream ref: AgTopology.Counts - Views and Groups differ exactly when an AG has more than
# one monitored reporter, which is why the header states both rather than just one.
_GROUPS_SQL = _count_sql(
    'COUNT(DISTINCT UPPER(latest.ag_name)) AS "Availability Groups"'
)
_SERVERS_SQL = _count_sql('COUNT(DISTINCT latest.server_id) AS "Reporting Servers"')
_VIEWS_SQL = _count_sql(
    'COUNT(DISTINCT (latest.server_id, UPPER(latest.ag_name))) AS "Views"'
)

# One row per AG (cluster), rolled up across every reporting server - the fleet-scale triage
# view. Only a reporter that IS the primary ever marks a role=PRIMARY row for itself (a
# secondary's own DMV row never does, per AgReplicaStatesCollector), so string_agg naturally
# yields exactly one name when any primary is monitored, and surfaces a genuine disagreement
# if more than one reporter ever claims it.
_REPLICA_AG_AGG = f"""
    replica_agg AS (
        SELECT
            UPPER(r.ag_name) AS ag_key,
            MAX(r.ag_name) AS ag_name,
            COUNT(DISTINCT r.server_id) AS reporting_servers,
            COUNT(DISTINCT r.replica_server_name) AS replica_count,
            STRING_AGG(
                DISTINCT CASE WHEN UPPER(r.role_desc) = 'PRIMARY'
                              THEN r.replica_server_name END,
                ', '
            ) AS primaries,
            MAX({_replica_severity_int('r')}) AS replica_worst_sev
        FROM replica_latest AS r
        WHERE r.ag_name IS NOT NULL AND {server_filter('r.server_id')}
        GROUP BY UPPER(r.ag_name)
    )
"""

# Left out of a card whose (server, AG) has no replica row - AgTopology.BuildCards drops the
# same case, reachable only when the two collectors' newest snapshots straddle an AG being
# created or dropped.
_DATABASE_AG_AGG = f"""
    database_agg AS (
        SELECT
            UPPER(d.ag_name) AS ag_key,
            COUNT(DISTINCT d.database_name) AS database_count,
            MAX({_db_sync_severity_int('d')}) AS database_worst_sev
        FROM database_latest AS d
        WHERE d.ag_name IS NOT NULL AND {server_filter('d.server_id')}
        GROUP BY UPPER(d.ag_name)
    )
"""

_SUMMARY_TABLE_SQL = f"""
WITH replica_latest AS ({_LATEST_REPLICA_CTE}),
database_latest AS ({_LATEST_DATABASE_CTE}),
{_REPLICA_AG_AGG},
{_DATABASE_AG_AGG}
SELECT
    ra.ag_name AS "AG",
    COALESCE(ra.primaries, 'No primary reported') AS "Primary",
    ra.reporting_servers AS "Reporting Servers",
    ra.replica_count AS "Replicas",
    COALESCE(da.database_count, 0) AS "Databases",
    {_severity_label('GREATEST(ra.replica_worst_sev, COALESCE(da.database_worst_sev, 0))')}
        AS "Severity"
FROM replica_agg ra
LEFT JOIN database_agg da ON da.ag_key = ra.ag_key
ORDER BY ra.ag_name
"""

_REPLICA_SQL = f"""
WITH latest AS ({_LATEST_REPLICA_CTE})
SELECT
    srv.name AS "Server",
    latest.ag_name AS "AG",
    latest.replica_server_name AS "Replica",
    {_tri('latest.is_local')} AS "Local",
    latest.role_desc AS "Role",
    latest.operational_state_desc AS "Operational State",
    latest.connected_state_desc AS "Connected State",
    latest.recovery_health_desc AS "Recovery Health",
    latest.synchronization_health_desc AS "Sync Health",
    latest.availability_mode_desc AS "Availability Mode",
    latest.failover_mode_desc AS "Failover Mode",
    latest.endpoint_url AS "Endpoint URL",
    {_severity_label(_replica_severity_int('latest'))} AS "Severity",
    latest.collection_time AS "Collected"
FROM latest
{server_join('latest.server_id')}
WHERE {server_filter('latest.server_id')}
  AND {_ag_filter('latest.ag_name')}
ORDER BY latest.ag_name, srv.name, latest.replica_server_name
"""

_DATABASE_SQL = f"""
WITH latest AS ({_LATEST_DATABASE_CTE})
SELECT
    srv.name AS "Server",
    latest.ag_name AS "AG",
    latest.database_name AS "Database",
    latest.replica_server_name AS "Replica",
    {_tri('latest.is_local')} AS "Local",
    latest.synchronization_state_desc AS "Sync State",
    latest.availability_mode_desc AS "Availability Mode",
    latest.log_send_queue_size AS "Send Queue (KB)",
    latest.redo_queue_size AS "Redo Queue (KB)",
    latest.log_send_rate AS "Send Rate (KB/s)",
    latest.redo_rate AS "Redo Rate (KB/s)",
    latest.secondary_lag_seconds AS "Lag (s)",
    {_drain_minutes('latest.log_send_queue_size', 'latest.log_send_rate')}
        AS "Est Send Drain (min)",
    {_drain_minutes('latest.redo_queue_size', 'latest.redo_rate')}
        AS "Est Redo Drain (min)",
    {_tri('latest.is_suspended')} AS "Suspended",
    latest.suspend_reason_desc AS "Suspend Reason",
    CASE
        WHEN latest.is_suspended THEN
            CASE WHEN NULLIF(TRIM(latest.suspend_reason_desc), '') IS NULL THEN 'Suspended'
                 ELSE 'Suspended: ' || latest.suspend_reason_desc END
        WHEN latest.is_suspended = false THEN 'Moving'
        ELSE '—'
    END AS "Data Movement",
    {_severity_label(_db_sync_severity_int('latest'))} AS "Sync Severity",
    latest.collection_time AS "Collected"
FROM latest
{server_join('latest.server_id')}
WHERE {server_filter('latest.server_id')}
  AND {_ag_filter('latest.ag_name')}
ORDER BY latest.ag_name, srv.name, latest.database_name, latest.replica_server_name
"""

_EMPTY_STATE_TEXT = """
No Availability Groups observed on any monitored server, until data is collected. The AG
collectors write no rows for an instance that hosts none, and they do not run against Azure
SQL Database - zero rows here is the normal result on most fleets.
"""

_COUNT_THRESHOLDS = thresholds(("text", None))


def availability_groups():
    """Build the Availability Groups dashboard: fleet-wide summary, one row per AG."""
    reset_id()
    panels: list[dict] = []

    y = subtab(
        panels,
        "Summary",
        0,
        [
            (
                24,
                2,
                lambda x, y, w, h: text_panel(
                    "No Availability Groups", x, y, w, h, _EMPTY_STATE_TEXT
                ),
            ),
            (
                8,
                4,
                lambda x, y, w, h: stat(
                    "Availability Groups",
                    x,
                    y,
                    w,
                    h,
                    _GROUPS_SQL,
                    "short",
                    _COUNT_THRESHOLDS,
                ),
            ),
            (
                8,
                4,
                lambda x, y, w, h: stat(
                    "Reporting Servers",
                    x,
                    y,
                    w,
                    h,
                    _SERVERS_SQL,
                    "short",
                    _COUNT_THRESHOLDS,
                ),
            ),
            (
                8,
                4,
                lambda x, y, w, h: stat(
                    "Views", x, y, w, h, _VIEWS_SQL, "short", _COUNT_THRESHOLDS
                ),
            ),
        ],
    )

    drill = col_datalink(
        "AG",
        "View replicas & databases",
        "/d/darling-availability-group-detail?${__url_time_range}"
        "&var-ag=${__data.fields.AG}",
    )

    subtab(
        panels,
        "Clusters",
        y,
        [
            (
                24,
                12,
                lambda x, y, w, h: table(
                    "Availability Groups",
                    x,
                    y,
                    w,
                    h,
                    _SUMMARY_TABLE_SQL,
                    overrides=[drill, status_colors("Severity", HEALTH_STATUS_COLORS)],
                    sort_by=[{"displayName": "Severity", "desc": True}],
                    description=(
                        "One row per AG, rolled up across every reporting server. Click "
                        "an AG to see its per-replica and per-database detail."
                    ),
                ),
            )
        ],
    )

    return dashboard(
        uid("availability-groups"), "Availability Groups", panels, [server_var(multi=True)]
    )


def availability_group_detail():
    """Build the Availability Group drill-down: per-replica and per-database detail."""
    reset_id()
    panels: list[dict] = []

    y = subtab(
        panels,
        "Replica Topology",
        0,
        [
            (
                24,
                10,
                lambda x, y, w, h: table(
                    "AG Replicas",
                    x,
                    y,
                    w,
                    h,
                    _REPLICA_SQL,
                    overrides=[status_colors("Severity", HEALTH_STATUS_COLORS)],
                    sort_by=[{"displayName": "Severity", "desc": True}],
                    description=(
                        "Latest snapshot per reporting server, not the dashboard time "
                        "range. One row per (reporting server, AG, replica) - a replica "
                        "with multiple monitored reporters appears once per reporter, "
                        "since each reports a different perspective."
                    ),
                ),
            )
        ],
    )

    subtab(
        panels,
        "Database Replication",
        y,
        [
            (
                24,
                12,
                lambda x, y, w, h: table(
                    "AG Database Replica States",
                    x,
                    y,
                    w,
                    h,
                    _DATABASE_SQL,
                    overrides=[
                        status_colors("Sync Severity", HEALTH_STATUS_COLORS),
                        col_unit("Est Send Drain (min)", "m"),
                        col_unit("Est Redo Drain (min)", "m"),
                        col_unit("Lag (s)", "s"),
                    ],
                    sort_by=[{"displayName": "Sync Severity", "desc": True}],
                    description=(
                        "Latest snapshot per reporting server. While suspended, the queue "
                        "and drain estimates freeze rather than grow - Suspended/Data "
                        "Movement carry that context, so a frozen-looking row is not "
                        "necessarily caught up."
                    ),
                ),
            )
        ],
    )

    return detail_dashboard(
        uid("availability-group-detail"),
        "Availability Group Detail",
        panels,
        [server_var(multi=True), text_var("ag", "Availability Group", "*")],
    )
