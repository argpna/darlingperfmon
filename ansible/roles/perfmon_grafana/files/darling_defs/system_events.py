"""System Events dashboard (Darling line).

Upstream ref: ViewerServerTab/ViewerDataService.SystemEvents.cs, SystemEventSignificance.cs
(Darling.Viewer); SystemHealthParser.cs, SystemHealthSignificance.cs,
DefaultTraceEventSignificance.cs (PerformanceMonitor.Common). Eleven sub-tabs in upstream's
XAML order; Significant Waits and Default Trace have no dashboard_defs precedent.

Every category but Corruption/Contention (unfiltered SYSTEM-component charts) is a
parse-on-read shred of event_xml, kept to the rows SystemHealthSignificance/
DefaultTraceEventSignificance call significant. Every event_xml::xml cast is guarded by
xml_is_well_formed_document() first, since Postgres has no tolerant-parse equivalent to
SystemHealthParser's CheckCharacters=false XmlReader.
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
    stat_grid,
    subtab,
    table,
    target,
    thresholds,
    timeseries,
    uid,
)

_TH_COUNT_RED = thresholds(("green", None), ("red", 1))
_TH_COUNT_WARN = thresholds(("transparent", None), ("#EAB839", 1))
_TH_NEUTRAL = thresholds(("blue", None))

# sp_HealthParser's #ignore_waits (SystemHealthSignificance.IgnoredWaitTypes) - the
# idle/queue/background wait types a discrete long wait_info event on is never actionable.
_IGNORED_WAIT_TYPES = (
    "ASYNC_IO_COMPLETION",
    "AZURE_IMDS_VERSIONS",
    "BROKER_EVENTHANDLER",
    "BROKER_RECEIVE_WAITFOR",
    "BROKER_TASK_STOP",
    "BROKER_TO_FLUSH",
    "BROKER_TRANSMITTER",
    "CHECKPOINT_QUEUE",
    "CHKPT",
    "CLR_AUTO_EVENT",
    "CLR_MANUAL_EVENT",
    "CLR_SEMAPHORE",
    "DBMIRROR_DBM_EVENT",
    "DBMIRROR_DBM_MUTEX",
    "DBMIRROR_EVENTS_QUEUE",
    "DBMIRROR_SEND",
    "DBMIRROR_WORKER_QUEUE",
    "DBMIRRORING_CMD",
    "DIRTY_PAGE_POLL",
    "DISPATCHER_QUEUE_SEMAPHORE",
    "FSAGENT",
    "FT_IFTS_SCHEDULER_IDLE_WAIT",
    "FT_IFTSHC_MUTEX",
    "HADR_CLUSAPI_CALL",
    "HADR_FILESTREAM_IOMGR_IOCOMPLETION",
    "HADR_LOGCAPTURE_WAIT",
    "HADR_NOTIFICATION_DEQUEUE",
    "HADR_TIMER_TASK",
    "HADR_WORK_QUEUE",
    "KSOURCE_WAKEUP",
    "LAZYWRITER_SLEEP",
    "LOGMGR_QUEUE",
    "MEMORY_ALLOCATION_EXT",
    "ONDEMAND_TASK_QUEUE",
    "PARALLEL_REDO_DRAIN_WORKER",
    "PARALLEL_REDO_LOG_CACHE",
    "PARALLEL_REDO_TRAN_LIST",
    "PARALLEL_REDO_WORKER_SYNC",
    "PARALLEL_REDO_WORKER_WAIT_WORK",
    "PREEMPTIVE_OS_FLUSHFILEBUFFERS",
    "PREEMPTIVE_XE_GETTARGETSTATE",
    "PVS_PREALLOCATE",
    "PWAIT_ALL_COMPONENTS_INITIALIZED",
    "PWAIT_DIRECTLOGCONSUMER_GETNEXT",
    "PWAIT_EXTENSIBILITY_CLEANUP_TASK",
    "QDS_ASYNC_QUEUE",
    "QDS_CLEANUP_STALE_QUERIES_TASK_MAIN_LOOP_SLEEP",
    "QDS_PERSIST_TASK_MAIN_LOOP_SLEEP",
    "QDS_SHUTDOWN_QUEUE",
    "REDO_THREAD_PENDING_WORK",
    "REQUEST_FOR_DEADLOCK_SEARCH",
    "RESOURCE_QUEUE",
    "SERVER_IDLE_CHECK",
    "SLEEP_DBSTARTUP",
    "SLEEP_DCOMSTARTUP",
    "SLEEP_MASTERDBREADY",
    "SLEEP_MASTERMDREADY",
    "SLEEP_MASTERUPGRADED",
    "SLEEP_MSDBSTARTUP",
    "SLEEP_SYSTEMTASK",
    "SLEEP_TEMPDBSTARTUP",
    "SNI_HTTP_ACCEPT",
    "SOS_WORK_DISPATCHER",
    "SP_SERVER_DIAGNOSTICS_SLEEP",
    "SQLTRACE_BUFFER_FLUSH",
    "SQLTRACE_INCREMENTAL_FLUSH_SLEEP",
    "SQLTRACE_WAIT_ENTRIES",
    "UCS_SESSION_REGISTRATION",
    "VDI_CLIENT_OTHER",
    "WAIT_FOR_RESULTS",
    "WAIT_XTP_CKPT_CLOSE",
    "WAIT_XTP_HOST_WAIT",
    "WAIT_XTP_OFFLINE_CKPT_NEW_LOG",
    "WAIT_XTP_RECOVERY",
    "WAITFOR",
    "WAITFOR_TASKSHUTDOWN",
    "XE_DISPATCHER_JOIN",
    "XE_DISPATCHER_WAIT",
    "XE_FILE_TARGET_TVF",
    "XE_LIVE_TARGET_TVF",
    "XE_TIMER_EVENT",
)
_IGNORED_WAIT_TYPES_SQL = ", ".join(f"'{w}'" for w in _IGNORED_WAIT_TYPES)

# sp_HealthParser always excludes these error_reported numbers (benign connection-reset
# noise) from severe errors, even past the severity floor.
_IGNORED_SEVERE_ERROR_NUMBERS_SQL = "17830, 18056"


def _events_cte(event_type: str) -> str:
    """This XE event type's raw rows for the window, guarded against a malformed cast."""
    return f"""events AS (
        SELECT e.server_id, e.event_time, e.event_xml::xml AS xml
        FROM {collector('system_health_events')} AS e
        WHERE e.event_type = '{event_type}'
          AND $__timeFilter(e.event_time)
          AND {server_filter('e.server_id')}
          AND xml_is_well_formed_document(e.event_xml)
    )"""


def _attr(name: str, node: str = "node") -> str:
    """Absolute-path attribute pull off an already-extracted xpath() fragment - a relative
    path does not resolve against a fragment root (verified in Phase 3's deadlock parse).
    """
    return f"""(xpath('/*/@{name}', {node}))[1]::text"""


def _data_value(name: str, ev: str = "ev.xml") -> str:
    """The <value> text of a <data name="..."> child, on the whole (unfragmented) event."""
    return f"""(xpath('/*/data[@name="{name}"]/value/text()', {ev}))[1]::text"""


def _data_text(name: str, ev: str = "ev.xml") -> str:
    """The <text> (friendly enum) of a <data name="..."> child."""
    return f"""(xpath('/*/data[@name="{name}"]/text/text()', {ev}))[1]::text"""


def _action_value(name: str, ev: str = "ev.xml") -> str:
    """The <value> text of an <action name="..."> child."""
    return f"""(xpath('/*/action[@name="{name}"]/value/text()', {ev}))[1]::text"""


def _bool_text(expr: str) -> str:
    """SQL Server bit text -> boolean, matching SystemHealthParser.ParseBool."""
    return f"""CASE WHEN {expr} IN ('1', 'true') THEN true
        WHEN {expr} IN ('0', 'false') THEN false END"""


def _entry(desc: str, xmlcol: str = "ev.xml") -> str:
    """A memoryReport/entry value by its @description - safe to search across the whole
    event since only the RESOURCE component's payload carries memoryReport nodes."""
    return f"""(xpath('//entry[@description="{desc}"]/@value', {xmlcol}))[1]::text"""


# Corruption Events / Contention Events (charts, no significance filter)
# Upstream ref: LoadSystemHealthChartsAsync / GetSystemHealthAsync (ViewerServerTab/
# ViewerDataService.SystemEvents.cs) - every collected SYSTEM-component snapshot, not just
# the significant ones; both sub-tabs read this one feed and pick their own columns.
def _system_health_series_sql(items) -> str:
    """A metric/value time series off the SYSTEM component's node attributes.
    items: (label, attribute) pairs."""
    values = ",\n        ".join(
        f"""('{label}', {_attr(attr)})""" for label, attr in items
    )
    return f"""
WITH {_events_cte('sp_server_diagnostics_component_result')},
nodes AS (
    SELECT ev.server_id, ev.event_time, node
    FROM events ev
    CROSS JOIN LATERAL unnest(xpath('//system', ev.xml)) AS node
)
SELECT
    n.event_time AS time,
    s.label AS metric,
    s.value::double precision AS value
FROM nodes n
CROSS JOIN LATERAL (VALUES
    {values}
) AS s(label, value)
ORDER BY 1
"""


_CORRUPTION_BAD_PAGES_SQL = _system_health_series_sql(
    [
        ("Bad Pages Detected", "BadPagesDetected"),
        ("Bad Pages Fixed", "BadPagesFixed"),
        ("Interval Dump Requests", "intervalDumpRequests"),
    ]
)

_CORRUPTION_ACCESS_VIOLATIONS_SQL = _system_health_series_sql(
    [
        ("Access Violation Occurred", "isAccessViolationOccurred"),
        ("Write Access Violations", "writeAccessViolationCount"),
    ]
)

_CONTENTION_NONYIELD_SQL = _system_health_series_sql(
    [
        ("Non-Yielding Tasks", "nonYieldingTasksReported"),
        ("Latch Warnings", "latchWarnings"),
        ("Spinlock Backoffs", "spinlockBackoffs"),
    ]
)

_CONTENTION_CPU_SQL = _system_health_series_sql(
    [
        ("SQL CPU %", "sqlCpuUtilization"),
        ("System CPU %", "systemCpuUtilization"),
    ]
)

_SICK_SPINLOCKS_SQL = f"""
WITH {_events_cte('sp_server_diagnostics_component_result')},
nodes AS (
    SELECT
        ev.server_id,
        ev.event_time,
        {_attr('sickSpinlockType')} AS sick_type,
        COALESCE({_attr('spinlockBackoffs')}::bigint, 1) AS backoffs
    FROM events ev
    CROSS JOIN LATERAL unnest(xpath('//system', ev.xml)) AS node
),
/* Postgres always carries the literal "none" rather than a null attribute when no sick
   spinlock was recorded - excluded here so the top 5 ranks real occurrences, matching the
   T-SQL precedent's IS NOT NULL intent. */
candidates AS (
    SELECT sick_type FROM nodes
    WHERE sick_type IS NOT NULL AND sick_type <> 'none'
    GROUP BY sick_type
    ORDER BY COUNT(*) DESC
    LIMIT 5
)
SELECT
    n.event_time AS time,
    n.sick_type AS metric,
    n.backoffs::double precision AS value
FROM nodes n
WHERE n.sick_type IN (SELECT sick_type FROM candidates)
ORDER BY 1
"""


# Scheduler Issues
# Upstream ref: GetSchedulerIssuesAsync (ViewerDataService.SystemEvents.cs);
# SystemHealthSignificance.IsSignificant(SchedulerIssueRecord) - status = 'WARNING'.
_SCHEDULER_ISSUES_SIGNIFICANT_SQL = f"""
WITH {_events_cte('scheduler_monitor_system_health_ring_buffer_recorded')}
SELECT
    ev.server_id,
    ev.event_time,
    {_data_value('scheduler_id')}::int AS scheduler_id,
    {_data_value('cpu_id')}::int AS cpu_id,
    {_data_text('status')} AS status,
    {_bool_text(_data_value('is_online'))} AS is_online,
    {_bool_text(_data_value('is_runnable'))} AS is_runnable,
    {_bool_text(_data_value('is_running'))} AS is_running,
    {_data_value('non_yielding_time')}::bigint AS non_yielding_time_ms,
    {_data_value('thread_quantum')}::bigint AS thread_quantum_ms
FROM events ev
"""

_SCHEDULER_ISSUES_TABLE_SQL = f"""
WITH sig AS ({_SCHEDULER_ISSUES_SIGNIFICANT_SQL})
SELECT
    sig.event_time AS "Event Time",
    srv.name AS "Server",
    sig.scheduler_id AS "Scheduler",
    sig.cpu_id AS "CPU",
    sig.status AS "Status",
    sig.is_online AS "Online",
    sig.is_runnable AS "Runnable",
    sig.is_running AS "Running",
    sig.non_yielding_time_ms AS "Non-Yield (ms)",
    sig.thread_quantum_ms AS "Thread Quantum (ms)"
FROM sig
{server_join('sig.server_id')}
WHERE sig.status = 'WARNING'
ORDER BY sig.event_time DESC
LIMIT 200
"""

_SCHEDULER_ISSUES_TREND_SQL = f"""
WITH sig AS ({_SCHEDULER_ISSUES_SIGNIFICANT_SQL})
SELECT
    sig.event_time AS time,
    'Scheduler ' || sig.scheduler_id AS metric,
    sig.non_yielding_time_ms::double precision AS value
FROM sig
WHERE sig.status = 'WARNING'
ORDER BY 1
"""

_SCHEDULER_ISSUES_STATS = [
    {
        "title": "Total Issues (range)",
        "sql": f"""WITH sig AS ({_SCHEDULER_ISSUES_SIGNIFICANT_SQL})
            SELECT COUNT(*) AS v FROM sig WHERE status = 'WARNING'""",
        "th": _TH_NEUTRAL,
    },
    {
        "title": "Total Non-Yield Time (range)",
        "sql": f"""WITH sig AS ({_SCHEDULER_ISSUES_SIGNIFICANT_SQL})
            SELECT COALESCE(SUM(non_yielding_time_ms), 0) AS v FROM sig WHERE status = 'WARNING'""",
        "unit": "ms",
        "th": _TH_NEUTRAL,
    },
    {
        "title": "Max Non-Yield Time (range)",
        "sql": f"""WITH sig AS ({_SCHEDULER_ISSUES_SIGNIFICANT_SQL})
            SELECT COALESCE(MAX(non_yielding_time_ms), 0) AS v FROM sig WHERE status = 'WARNING'""",
        "unit": "ms",
        "th": _TH_NEUTRAL,
    },
    {
        "title": "Distinct Schedulers Affected",
        "sql": f"""WITH sig AS ({_SCHEDULER_ISSUES_SIGNIFICANT_SQL})
            SELECT COUNT(DISTINCT scheduler_id) AS v FROM sig WHERE status = 'WARNING'""",
        "th": _TH_NEUTRAL,
    },
    {
        "title": "Offline Scheduler Events",
        "sql": f"""WITH sig AS ({_SCHEDULER_ISSUES_SIGNIFICANT_SQL})
            SELECT COUNT(*) AS v FROM sig WHERE status = 'WARNING' AND is_online = false""",
        "th": _TH_COUNT_RED,
    },
]


# Severe Errors
# Upstream ref: GetSevereErrorsAsync + ResolveDatabaseName (ViewerDataService.
# SystemEvents.cs); SystemHealthSignificance.IsSignificant(SevereErrorRecord) -
# severity >= 19 excluding the benign connection-reset numbers.
_SEVERE_ERRORS_SIGNIFICANT_SQL = f"""
WITH {_events_cte('error_reported')},
parsed AS (
    SELECT
        ev.server_id,
        ev.event_time,
        {_data_value('error_number')}::int AS error_number,
        {_data_value('severity')}::int AS severity,
        {_data_value('state')}::int AS state,
        {_data_value('database_id')}::int AS database_id,
        {_data_value('message')} AS message
    FROM events ev
),
/* database_size_stats carries both database_id and database_name for every online
   database - the same mapping source ResolveDatabaseName uses. */
dbmap AS (
    SELECT DISTINCT ON (d.server_id, d.database_id)
        d.server_id, d.database_id, d.database_name
    FROM {collector('database_size_stats')} AS d
    ORDER BY d.server_id, d.database_id, d.collection_time DESC
)
SELECT
    p.server_id,
    p.event_time,
    p.error_number,
    p.severity,
    p.state,
    p.message,
    COALESCE(
        m.database_name,
        CASE WHEN p.database_id IS NULL OR p.database_id = 0
            THEN '' ELSE 'database_id ' || p.database_id END
    ) AS resolved_database_name
FROM parsed p
LEFT JOIN dbmap m ON m.server_id = p.server_id AND m.database_id = p.database_id
"""

_SEVERE_ERRORS_WHERE = f"""sig.severity >= 19
  AND (sig.error_number IS NULL OR sig.error_number NOT IN ({_IGNORED_SEVERE_ERROR_NUMBERS_SQL}))
  AND {multi_filter('sig.resolved_database_name', 'database')}"""

_SEVERE_ERRORS_TABLE_SQL = f"""
WITH sig AS ({_SEVERE_ERRORS_SIGNIFICANT_SQL})
SELECT
    sig.event_time AS "Event Time",
    srv.name AS "Server",
    sig.error_number AS "Error #",
    sig.severity AS "Severity",
    sig.state AS "State",
    sig.resolved_database_name AS "Database",
    sig.message AS "Message"
FROM sig
{server_join('sig.server_id')}
WHERE {_SEVERE_ERRORS_WHERE}
ORDER BY sig.event_time DESC
LIMIT 200
"""

_SEVERE_ERRORS_TREND_SQL = f"""
WITH sig AS ({_SEVERE_ERRORS_SIGNIFICANT_SQL})
SELECT
    date_trunc('hour', sig.event_time) AS time,
    'Severe Errors' AS metric,
    COUNT(*)::double precision AS value
FROM sig
WHERE {_SEVERE_ERRORS_WHERE}
GROUP BY 1, 2
ORDER BY 1
"""


# Memory Conditions
# Upstream ref: GetMemoryConditionsAsync (ViewerDataService.SystemEvents.cs);
# SystemHealthSignificance.IsSignificant(MemoryConditionsRecord) -
# lastNotification = 'RESOURCE_MEMPHYSICAL_LOW'.
_MEMORY_CONDITIONS_SIGNIFICANT_SQL = f"""
WITH {_events_cte('sp_server_diagnostics_component_result')},
resources AS (
    SELECT ev.server_id, ev.event_time, ev.xml,
        {_attr('lastNotification')} AS last_notification,
        {_attr('outOfMemoryExceptions')}::bigint AS out_of_memory_exceptions,
        {_bool_text(_attr('isAnyPoolOutOfMemory'))} AS is_any_pool_out_of_memory,
        {_attr('processOutOfMemoryPeriod')}::bigint AS process_out_of_memory_period
    FROM events ev
    CROSS JOIN LATERAL unnest(xpath('//resource', ev.xml)) AS node
)
SELECT
    r.server_id,
    r.event_time,
    (xpath('//memoryReport/@name', r.xml))[1]::text AS name,
    r.last_notification,
    r.out_of_memory_exceptions,
    r.is_any_pool_out_of_memory,
    r.process_out_of_memory_period,
    {_entry('Available Physical Memory', 'r.xml')}::bigint / 1073741824 AS available_physical_memory_gb,
    {_entry('Available Virtual Memory', 'r.xml')}::bigint / 1073741824 AS available_virtual_memory_gb,
    {_entry('Available Paging File', 'r.xml')}::bigint / 1073741824 AS available_paging_file_gb,
    {_entry('Working Set', 'r.xml')}::bigint / 1073741824 AS working_set_gb,
    {_entry('Percent of Committed Memory in WS', 'r.xml')}::bigint AS percent_of_committed_memory_in_ws,
    {_entry('Page Faults', 'r.xml')}::bigint AS page_faults,
    {_entry('System physical memory high', 'r.xml')}::bigint AS system_physical_memory_high,
    {_entry('System physical memory low', 'r.xml')}::bigint AS system_physical_memory_low,
    {_entry('Process physical memory low', 'r.xml')}::bigint AS process_physical_memory_low,
    {_entry('Process virtual memory low', 'r.xml')}::bigint AS process_virtual_memory_low,
    {_entry('VM Reserved', 'r.xml')}::bigint / 1048576 AS vm_reserved_gb,
    {_entry('VM Committed', 'r.xml')}::bigint / 1048576 AS vm_committed_gb,
    {_entry('Target Committed', 'r.xml')}::bigint / 1048576 AS target_committed_gb,
    {_entry('Current Committed', 'r.xml')}::bigint / 1048576 AS current_committed_gb
FROM resources r
"""

_MEMORY_CONDITIONS_TABLE_SQL = f"""
WITH sig AS ({_MEMORY_CONDITIONS_SIGNIFICANT_SQL})
SELECT
    sig.event_time AS "Event Time",
    srv.name AS "Server",
    sig.name AS "Name",
    sig.last_notification AS "Last Notification",
    sig.out_of_memory_exceptions AS "OOM Exceptions",
    sig.is_any_pool_out_of_memory AS "Any Pool OOM",
    sig.process_out_of_memory_period AS "Process OOM Period",
    sig.available_physical_memory_gb AS "Avail Physical (GB)",
    sig.available_virtual_memory_gb AS "Avail Virtual (GB)",
    sig.available_paging_file_gb AS "Avail Paging File (GB)",
    sig.working_set_gb AS "Working Set (GB)",
    sig.percent_of_committed_memory_in_ws AS "% Committed in WS",
    sig.page_faults AS "Page Faults",
    sig.system_physical_memory_high AS "Sys Physical Mem High",
    sig.system_physical_memory_low AS "Sys Physical Mem Low",
    sig.process_physical_memory_low AS "Process Physical Mem Low",
    sig.process_virtual_memory_low AS "Process Virtual Mem Low",
    sig.vm_reserved_gb AS "VM Reserved (GB)",
    sig.vm_committed_gb AS "VM Committed (GB)",
    sig.target_committed_gb AS "Target Committed (GB)",
    sig.current_committed_gb AS "Current Committed (GB)"
FROM sig
{server_join('sig.server_id')}
WHERE sig.last_notification = 'RESOURCE_MEMPHYSICAL_LOW'
ORDER BY sig.event_time DESC
LIMIT 100
"""

_MEMORY_CONDITIONS_TREND_SQL = f"""
WITH sig AS ({_MEMORY_CONDITIONS_SIGNIFICANT_SQL})
SELECT
    date_trunc('hour', sig.event_time) AS time,
    'OOM Exceptions' AS metric,
    SUM(sig.out_of_memory_exceptions)::double precision AS value
FROM sig
WHERE sig.last_notification = 'RESOURCE_MEMPHYSICAL_LOW'
GROUP BY 1, 2
ORDER BY 1
"""


# Memory Broker
# Upstream ref: GetMemoryBrokerAsync (ViewerDataService.SystemEvents.cs);
# SystemHealthSignificance.IsSignificant(MemoryBrokerRecord) -
# notification = 'RESOURCE_MEMPHYSICAL_LOW'.
_MEMORY_BROKER_SIGNIFICANT_SQL = f"""
WITH {_events_cte('memory_broker_ring_buffer_recorded')}
SELECT
    ev.server_id,
    ev.event_time,
    {_data_value('id')}::bigint AS broker_id,
    {_data_value('pool_metadata_id')}::bigint AS pool_metadata_id,
    {_data_value('delta_time')}::bigint AS delta_time,
    {_data_value('memory_ratio')}::bigint AS memory_ratio,
    {_data_value('new_target')}::bigint AS new_target,
    {_data_value('overall')}::bigint AS overall,
    {_data_value('rate')}::bigint AS rate,
    {_data_value('currently_predicated')}::bigint AS currently_predicated,
    {_data_value('currently_allocated')}::bigint AS currently_allocated,
    {_data_value('previously_allocated')}::bigint AS previously_allocated,
    {_data_value('broker')} AS broker,
    {_data_value('notification')} AS notification
FROM events ev
"""

_MEMORY_BROKER_TABLE_SQL = f"""
WITH sig AS ({_MEMORY_BROKER_SIGNIFICANT_SQL})
SELECT
    sig.event_time AS "Event Time",
    srv.name AS "Server",
    sig.broker_id AS "Broker ID",
    sig.pool_metadata_id AS "Pool Metadata ID",
    sig.delta_time AS "Delta Time",
    sig.broker AS "Broker",
    sig.notification AS "Notification",
    sig.memory_ratio AS "Memory Ratio",
    sig.new_target AS "New Target",
    sig.overall AS "Overall",
    sig.rate AS "Rate",
    sig.currently_predicated AS "Currently Predicated",
    sig.currently_allocated AS "Currently Allocated",
    sig.previously_allocated AS "Previously Allocated"
FROM sig
{server_join('sig.server_id')}
WHERE sig.notification = 'RESOURCE_MEMPHYSICAL_LOW'
ORDER BY sig.event_time DESC
LIMIT 200
"""

_MEMORY_BROKER_TREND_SQL = f"""
WITH sig AS ({_MEMORY_BROKER_SIGNIFICANT_SQL})
SELECT
    sig.event_time AS time,
    sig.broker AS metric,
    sig.currently_allocated::double precision AS value
FROM sig
WHERE sig.notification = 'RESOURCE_MEMPHYSICAL_LOW'
ORDER BY 1
"""


# Memory Node OOM
# Upstream ref: GetMemoryNodeOomAsync (ViewerDataService.SystemEvents.cs);
# SystemHealthSignificance.IsSignificant(MemoryNodeOomRecord) - every recorded OOM is
# significant, sp_HealthParser applies no WHERE filter to this category.
_MEMORY_NODE_OOM_SQL = f"""
WITH {_events_cte('memory_node_oom_ring_buffer_recorded')}
SELECT
    ev.server_id,
    ev.event_time,
    {_data_value('id')}::bigint AS node_id,
    {_data_value('memory_node_id')}::bigint AS memory_node_id,
    {_data_value('memory_utilization_pct')}::bigint AS memory_utilization_pct,
    {_data_value('total_physical_memory_kb')}::bigint AS total_physical_memory_kb,
    {_data_value('available_physical_memory_kb')}::bigint AS available_physical_memory_kb,
    {_data_value('total_page_file_kb')}::bigint AS total_page_file_kb,
    {_data_value('available_page_file_kb')}::bigint AS available_page_file_kb,
    {_data_value('total_virtual_address_space_kb')}::bigint AS total_virtual_address_space_kb,
    {_data_value('available_virtual_address_space_kb')}::bigint AS available_virtual_address_space_kb,
    {_data_value('target_kb')}::bigint AS target_kb,
    {_data_value('reserved_kb')}::bigint AS reserved_kb,
    {_data_value('committed_kb')}::bigint AS committed_kb,
    {_data_value('awe_kb')}::bigint AS awe_kb,
    {_data_value('pages_kb')}::bigint AS pages_kb,
    {_data_text('failure')} AS failure_type,
    {_data_value('failure')}::bigint AS failure_value,
    {_data_value('resources')}::bigint AS resources,
    {_data_text('factor')} AS factor_text,
    {_data_value('factor')}::bigint AS factor_value,
    {_data_value('last_error')}::bigint AS last_error,
    {_data_value('pool_metadata_id')}::bigint AS pool_metadata_id,
    {_data_value('is_process_in_job')} AS is_process_in_job,
    {_data_value('is_system_physical_memory_high')} AS is_system_physical_memory_high,
    {_data_value('is_system_physical_memory_low')} AS is_system_physical_memory_low,
    {_data_value('is_process_physical_memory_low')} AS is_process_physical_memory_low,
    {_data_value('is_process_virtual_memory_low')} AS is_process_virtual_memory_low
FROM events ev
"""

_MEMORY_NODE_OOM_TABLE_SQL = f"""
WITH sig AS ({_MEMORY_NODE_OOM_SQL})
SELECT
    sig.event_time AS "Event Time",
    srv.name AS "Server",
    sig.node_id AS "Node ID",
    sig.memory_node_id AS "Memory Node ID",
    sig.memory_utilization_pct AS "Utilization %",
    (sig.total_physical_memory_kb / 1048576.0)::numeric(18,2) AS "Total Physical (GB)",
    (sig.available_physical_memory_kb / 1048576.0)::numeric(18,2) AS "Avail Physical (GB)",
    (sig.total_page_file_kb / 1048576.0)::numeric(18,2) AS "Total Page File (GB)",
    (sig.available_page_file_kb / 1048576.0)::numeric(18,2) AS "Avail Page File (GB)",
    (sig.total_virtual_address_space_kb / 1048576.0)::numeric(18,2) AS "Total VAS (GB)",
    (sig.available_virtual_address_space_kb / 1048576.0)::numeric(18,2) AS "Avail VAS (GB)",
    (sig.target_kb / 1048576.0)::numeric(18,2) AS "Target (GB)",
    (sig.reserved_kb / 1048576.0)::numeric(18,2) AS "Reserved (GB)",
    (sig.committed_kb / 1048576.0)::numeric(18,2) AS "Committed (GB)",
    (sig.awe_kb / 1048576.0)::numeric(18,2) AS "AWE (GB)",
    (sig.pages_kb / 1048576.0)::numeric(18,2) AS "Pages (GB)",
    sig.failure_type AS "Failure Type",
    sig.failure_value AS "Failure Value",
    sig.resources AS "Resources",
    sig.factor_text AS "Factor",
    sig.factor_value AS "Factor Value",
    sig.last_error AS "Last Error",
    sig.pool_metadata_id AS "Pool Metadata ID",
    sig.is_process_in_job AS "Process In Job",
    sig.is_system_physical_memory_high AS "Sys Physical Mem High",
    sig.is_system_physical_memory_low AS "Sys Physical Mem Low",
    sig.is_process_physical_memory_low AS "Process Physical Mem Low",
    sig.is_process_virtual_memory_low AS "Process Virtual Mem Low"
FROM sig
{server_join('sig.server_id')}
ORDER BY sig.event_time DESC
LIMIT 100
"""

_MEMORY_NODE_OOM_UTILIZATION_SQL = f"""
WITH sig AS ({_MEMORY_NODE_OOM_SQL})
SELECT
    sig.event_time AS time,
    'Node ' || sig.memory_node_id AS metric,
    sig.memory_utilization_pct::double precision AS value
FROM sig
ORDER BY 1
"""

_MEMORY_NODE_OOM_BREAKDOWN_SQL = f"""
WITH sig AS ({_MEMORY_NODE_OOM_SQL})
SELECT
    sig.event_time AS time,
    s.label AS metric,
    s.value / 1048576.0 AS value
FROM sig
CROSS JOIN LATERAL (VALUES
    ('Target', sig.target_kb),
    ('Committed', sig.committed_kb),
    ('Total Page File', sig.total_page_file_kb),
    ('Avail Page File', sig.available_page_file_kb)
) AS s(label, value)
ORDER BY 1
"""

_MEMORY_NODE_OOM_TREND_SQL = f"""
WITH sig AS ({_MEMORY_NODE_OOM_SQL})
SELECT
    date_trunc('hour', sig.event_time) AS time,
    'OOM Events' AS metric,
    COUNT(*)::double precision AS value
FROM sig
GROUP BY 1, 2
ORDER BY 1
"""

_MEMORY_NODE_OOM_STATS = [
    {
        "title": "Sys Memory High (range)",
        "sql": f"""WITH sig AS ({_MEMORY_NODE_OOM_SQL})
            SELECT COUNT(*) AS v FROM sig WHERE LOWER(is_system_physical_memory_high) = 'true'""",
        "th": thresholds(("transparent", None), ("green", 1)),
    },
    {
        "title": "Sys Memory Low (range)",
        "sql": f"""WITH sig AS ({_MEMORY_NODE_OOM_SQL})
            SELECT COUNT(*) AS v FROM sig WHERE LOWER(is_system_physical_memory_low) = 'true'""",
        "th": thresholds(("transparent", None), ("red", 1)),
    },
    {
        "title": "Process Memory Low (range)",
        "sql": f"""WITH sig AS ({_MEMORY_NODE_OOM_SQL})
            SELECT COUNT(*) AS v FROM sig WHERE LOWER(is_process_physical_memory_low) = 'true'""",
        "th": thresholds(("transparent", None), ("red", 1)),
    },
    {
        "title": "Process Virtual Memory Low (range)",
        "sql": f"""WITH sig AS ({_MEMORY_NODE_OOM_SQL})
            SELECT COUNT(*) AS v FROM sig WHERE LOWER(is_process_virtual_memory_low) = 'true'""",
        "th": _TH_COUNT_WARN,
    },
]


# Significant Waits (Darling-only, no dashboard_defs precedent)
# Upstream ref: GetSignificantWaitsAsync (ViewerDataService.SystemEvents.cs);
# SystemHealthSignificance.IsSignificant(SignificantWaitRecord) - real session, non-BACKUP
# statement, duration >= 500 ms, wait type not on the idle-wait ignore list.
_SIGNIFICANT_WAITS_SIGNIFICANT_SQL = f"""
WITH {_events_cte('wait_info')}
SELECT
    ev.server_id,
    ev.event_time,
    {_data_text('wait_type')} AS wait_type,
    {_data_value('duration')}::bigint AS duration_ms,
    {_data_value('signal_duration')}::bigint AS signal_duration_ms,
    {_data_value('wait_resource')} AS wait_resource,
    {_action_value('session_id')}::int AS session_id,
    {_action_value('sql_text')} AS query_text
FROM events ev
"""

# No CleanSqlText equivalent - see module docstring.
_SIGNIFICANT_WAITS_WHERE = f"""sig.session_id <> 0
  AND sig.query_text IS NOT NULL AND sig.query_text <> ''
  AND sig.query_text NOT ILIKE '%BACKUP%'
  AND sig.duration_ms >= 500
  AND sig.wait_type NOT IN ({_IGNORED_WAIT_TYPES_SQL})"""

_SIGNIFICANT_WAITS_TABLE_SQL = f"""
WITH sig AS ({_SIGNIFICANT_WAITS_SIGNIFICANT_SQL})
SELECT
    sig.event_time AS "Event Time",
    srv.name AS "Server",
    sig.wait_type AS "Wait Type",
    sig.duration_ms AS "Duration (ms)",
    sig.signal_duration_ms AS "Signal Duration (ms)",
    sig.wait_resource AS "Wait Resource",
    sig.session_id AS "Session ID",
    sig.query_text AS "SQL Text"
FROM sig
{server_join('sig.server_id')}
WHERE {_SIGNIFICANT_WAITS_WHERE}
ORDER BY sig.event_time DESC
LIMIT 200
"""

_SIGNIFICANT_WAITS_TREND_SQL = f"""
WITH sig AS ({_SIGNIFICANT_WAITS_SIGNIFICANT_SQL})
SELECT
    date_trunc('hour', sig.event_time) AS time,
    'Significant Waits' AS metric,
    COUNT(*)::double precision AS value
FROM sig
WHERE {_SIGNIFICANT_WAITS_WHERE}
GROUP BY 1, 2
ORDER BY 1
"""

_SIGNIFICANT_WAITS_STATS = [
    {
        "title": "Significant Waits (range)",
        "sql": f"""WITH sig AS ({_SIGNIFICANT_WAITS_SIGNIFICANT_SQL})
            SELECT COUNT(*) AS v FROM sig WHERE {_SIGNIFICANT_WAITS_WHERE.replace('sig.', '')}""",
        "th": _TH_NEUTRAL,
    },
    {
        "title": "Total Wait Time (range)",
        "sql": f"""WITH sig AS ({_SIGNIFICANT_WAITS_SIGNIFICANT_SQL})
            SELECT COALESCE(SUM(duration_ms), 0) AS v FROM sig WHERE {_SIGNIFICANT_WAITS_WHERE.replace('sig.', '')}""",
        "unit": "ms",
        "th": _TH_NEUTRAL,
    },
    {
        "title": "Max Wait (range)",
        "sql": f"""WITH sig AS ({_SIGNIFICANT_WAITS_SIGNIFICANT_SQL})
            SELECT COALESCE(MAX(duration_ms), 0) AS v FROM sig WHERE {_SIGNIFICANT_WAITS_WHERE.replace('sig.', '')}""",
        "unit": "ms",
        "th": _TH_NEUTRAL,
    },
    {
        "title": "Distinct Wait Types",
        "sql": f"""WITH sig AS ({_SIGNIFICANT_WAITS_SIGNIFICANT_SQL})
            SELECT COUNT(DISTINCT wait_type) AS v FROM sig WHERE {_SIGNIFICANT_WAITS_WHERE.replace('sig.', '')}""",
        "th": _TH_NEUTRAL,
    },
]


# CPU Tasks
# Upstream ref: GetCpuTasksAsync (ViewerDataService.SystemEvents.cs);
# SystemHealthSignificance.IsSignificant(CpuTasksRecord) - QUERY_PROCESSING state
# WARNING and pendingTasks >= 10.
_CPU_TASKS_SIGNIFICANT_SQL = f"""
WITH {_events_cte('sp_server_diagnostics_component_result')},
nodes AS (
    SELECT ev.server_id, ev.event_time, ev.xml, node
    FROM events ev
    CROSS JOIN LATERAL unnest(xpath('//queryProcessing', ev.xml)) AS node
)
SELECT
    n.server_id,
    n.event_time,
    {_data_text('state', 'n.xml')} AS state,
    {_attr('maxWorkers')}::bigint AS max_workers,
    {_attr('workersCreated')}::bigint AS workers_created,
    {_attr('workersIdle')}::bigint AS workers_idle,
    {_attr('tasksCompletedWithinInterval')}::bigint AS tasks_completed_within_interval,
    {_attr('pendingTasks')}::bigint AS pending_tasks,
    {_attr('oldestPendingTaskWaitingTime')}::bigint AS oldest_pending_task_waiting_time,
    {_bool_text(_attr('hasUnresolvableDeadlockOccurred'))} AS has_unresolvable_deadlock_occurred,
    {_bool_text(_attr('hasDeadlockedSchedulersOccurred'))} AS has_deadlocked_schedulers_occurred,
    cardinality(xpath('/*/blockingTasks/blocked-process-report', n.node)) > 0 AS did_blocking_occur
FROM nodes n
"""

_CPU_TASKS_WHERE = "sig.state = 'WARNING' AND sig.pending_tasks >= 10"

_CPU_TASKS_TABLE_SQL = f"""
WITH sig AS ({_CPU_TASKS_SIGNIFICANT_SQL})
SELECT
    sig.event_time AS "Event Time",
    srv.name AS "Server",
    sig.state AS "State",
    sig.max_workers AS "Max Workers",
    sig.workers_created AS "Workers Created",
    sig.workers_idle AS "Workers Idle",
    sig.tasks_completed_within_interval AS "Tasks Completed",
    sig.pending_tasks AS "Pending Tasks",
    sig.oldest_pending_task_waiting_time AS "Oldest Pending Wait",
    sig.has_unresolvable_deadlock_occurred AS "Unresolvable Deadlock",
    sig.has_deadlocked_schedulers_occurred AS "Deadlocked Schedulers",
    sig.did_blocking_occur AS "Blocking Occurred"
FROM sig
{server_join('sig.server_id')}
WHERE {_CPU_TASKS_WHERE}
ORDER BY sig.event_time DESC
LIMIT 200
"""

_CPU_TASKS_WORKERS_SQL = f"""
WITH sig AS ({_CPU_TASKS_SIGNIFICANT_SQL})
SELECT
    sig.event_time AS time,
    s.label AS metric,
    s.value::double precision AS value
FROM sig
CROSS JOIN LATERAL (VALUES
    ('Workers Created', sig.workers_created),
    ('Workers Idle', sig.workers_idle),
    ('Max Workers', sig.max_workers)
) AS s(label, value)
WHERE {_CPU_TASKS_WHERE}
ORDER BY 1
"""

_CPU_TASKS_PENDING_SQL = f"""
WITH sig AS ({_CPU_TASKS_SIGNIFICANT_SQL})
SELECT
    sig.event_time AS time,
    s.label AS metric,
    s.value::double precision AS value
FROM sig
CROSS JOIN LATERAL (VALUES
    ('Pending Tasks', sig.pending_tasks),
    ('Oldest Pending Wait (ms)', sig.oldest_pending_task_waiting_time)
) AS s(label, value)
WHERE {_CPU_TASKS_WHERE}
ORDER BY 1
"""

_CPU_TASKS_STATS = [
    {
        "title": "Unresolvable Deadlocks (range)",
        "sql": f"""WITH sig AS ({_CPU_TASKS_SIGNIFICANT_SQL})
            SELECT COUNT(*) AS v FROM sig WHERE {_CPU_TASKS_WHERE} AND has_unresolvable_deadlock_occurred""",
        "th": _TH_COUNT_RED,
    },
    {
        "title": "Scheduler Deadlocks (range)",
        "sql": f"""WITH sig AS ({_CPU_TASKS_SIGNIFICANT_SQL})
            SELECT COUNT(*) AS v FROM sig WHERE {_CPU_TASKS_WHERE} AND has_deadlocked_schedulers_occurred""",
        "th": _TH_COUNT_RED,
    },
    {
        "title": "Blocking Events (range)",
        "sql": f"""WITH sig AS ({_CPU_TASKS_SIGNIFICANT_SQL})
            SELECT COUNT(*) AS v FROM sig WHERE {_CPU_TASKS_WHERE} AND did_blocking_occur""",
        "th": _TH_NEUTRAL,
    },
    {
        "title": "Pending w/o Blocking (range)",
        "sql": f"""WITH sig AS ({_CPU_TASKS_SIGNIFICANT_SQL})
            SELECT COUNT(*) AS v FROM sig WHERE {_CPU_TASKS_WHERE} AND NOT did_blocking_occur""",
        "th": _TH_NEUTRAL,
    },
]


# I/O Issues
# Upstream ref: GetIoIssuesAsync (ViewerDataService.SystemEvents.cs);
# SystemHealthSignificance.IsSignificant(IoIssuesRecord) - IO_SUBSYSTEM state WARNING.
# One row per distinct pendingRequest filePath, durations summed - sp_HealthParser's
# per-event fan-out (SystemHealthParser.ParseIoIssues).
_IO_ISSUES_SIGNIFICANT_SQL = f"""
WITH {_events_cte('sp_server_diagnostics_component_result')},
nodes AS (
    SELECT ev.server_id, ev.event_time, ev.xml, node
    FROM events ev
    CROSS JOIN LATERAL unnest(xpath('//ioSubsystem', ev.xml)) AS node
),
pending AS (
    SELECT
        n.server_id,
        n.event_time,
        {_data_text('state', 'n.xml')} AS state,
        {_attr('ioLatchTimeouts')}::bigint AS io_latch_timeouts,
        {_attr('intervalLongIos')}::bigint AS interval_long_ios,
        {_attr('totalLongIos')}::bigint AS total_long_ios,
        COALESCE((xpath('/*/@filePath', pr))[1]::text, 'N/A') AS file_path,
        (xpath('/*/@duration', pr))[1]::text::bigint AS duration
    FROM nodes n
    CROSS JOIN LATERAL unnest(xpath('/*/longestPendingRequests/pendingRequest', n.node)) AS pr
)
SELECT
    server_id, event_time, state, io_latch_timeouts, interval_long_ios, total_long_ios,
    file_path,
    SUM(duration) AS longest_pending_requests_duration_ms
FROM pending
WHERE duration IS NOT NULL
GROUP BY server_id, event_time, state, io_latch_timeouts, interval_long_ios, total_long_ios, file_path
"""

_IO_ISSUES_TABLE_SQL = f"""
WITH sig AS ({_IO_ISSUES_SIGNIFICANT_SQL})
SELECT
    sig.event_time AS "Event Time",
    srv.name AS "Server",
    sig.state AS "State",
    sig.io_latch_timeouts AS "I/O Latch Timeouts",
    sig.interval_long_ios AS "Interval Long I/Os",
    sig.total_long_ios AS "Total Long I/Os",
    sig.longest_pending_requests_duration_ms AS "Duration (ms)",
    sig.file_path AS "File Path"
FROM sig
{server_join('sig.server_id')}
WHERE sig.state = 'WARNING'
ORDER BY sig.event_time DESC
LIMIT 100
"""

_IO_ISSUES_TREND_SQL = f"""
WITH sig AS ({_IO_ISSUES_SIGNIFICANT_SQL})
SELECT
    sig.event_time AS time,
    s.label AS metric,
    s.value::double precision AS value
FROM sig
CROSS JOIN LATERAL (VALUES
    ('I/O Latch Timeouts', sig.io_latch_timeouts),
    ('Interval Long I/Os', sig.interval_long_ios)
) AS s(label, value)
WHERE sig.state = 'WARNING'
ORDER BY 1
"""


# Default Trace (Darling-only, no dashboard_defs precedent)
# Upstream ref: GetDefaultTraceEventsAsync (ViewerDataService.SystemEvents.cs);
# DefaultTraceEventSignificance.Classify / IsSignificant (PerformanceMonitor.Common) -
# ErrorLog gated by severity >= 16, every other collected category significant as-is.
#
# StartTime is server-local; de-skewed to naive-UTC via server_properties.utc_offset_minutes,
# joined per-server rather than upstream's single-server scalar lookup.
_DEFAULT_TRACE_CATEGORY_CASE = """CASE
        WHEN c.event_name ILIKE '%Server Memory Change%' THEN 'Memory Change'
        WHEN c.event_name ILIKE '%Auto Grow%' OR c.event_name ILIKE '%Auto Shrink%' THEN 'Auto Grow/Shrink'
        WHEN c.event_name ILIKE 'ErrorLog' THEN 'Error Log'
        WHEN c.event_name ILIKE ANY(ARRAY['Object:Created', 'Object:Altered', 'Object:Deleted']) THEN 'Schema Change'
        WHEN c.event_name ILIKE ANY(ARRAY['Audit Change Audit Event', 'Audit DBCC Event', 'Audit Server Alter Trace Event']) THEN 'Security Audit'
        ELSE 'Other'
    END"""

_DEFAULT_TRACE_SIGNIFICANT_SQL = f"""
WITH svr_offsets AS (
    SELECT DISTINCT ON (sp.server_id) sp.server_id, sp.utc_offset_minutes
    FROM {collector('server_properties')} AS sp
    WHERE sp.utc_offset_minutes IS NOT NULL
    ORDER BY sp.server_id, sp.collection_time DESC
),
events AS (
    SELECT
        dte.server_id,
        dte.event_time - make_interval(mins => COALESCE(o.utc_offset_minutes, 0)) AS event_time_utc,
        dte.event_name,
        dte.database_name,
        dte.object_name,
        dte.login_name,
        dte.host_name,
        dte.application_name,
        dte.spid,
        dte.duration_us,
        dte.integer_data,
        dte.severity,
        dte.error_number,
        dte.text_data
    FROM {collector('default_trace_events')} AS dte
    LEFT JOIN svr_offsets o ON o.server_id = dte.server_id
    WHERE {server_filter('dte.server_id')}
),
categorized AS (
    SELECT c.*, {_DEFAULT_TRACE_CATEGORY_CASE} AS category
    FROM events c
    WHERE $__timeFilter(c.event_time_utc)
)
SELECT * FROM categorized
"""

_DEFAULT_TRACE_WHERE = f"""(sig.category <> 'Error Log' OR sig.severity IS NULL OR sig.severity >= 16)
  AND {multi_filter('sig.database_name', 'database')}"""

_DEFAULT_TRACE_TABLE_SQL = f"""
WITH sig AS ({_DEFAULT_TRACE_SIGNIFICANT_SQL})
SELECT
    sig.event_time_utc AS "Event Time",
    srv.name AS "Server",
    sig.category AS "Category",
    sig.event_name AS "Event",
    sig.database_name AS "Database",
    sig.object_name AS "Object",
    sig.login_name AS "Login",
    sig.host_name AS "Host",
    sig.application_name AS "App",
    sig.spid AS "SPID",
    CASE WHEN sig.category = 'Auto Grow/Shrink' AND sig.duration_us IS NOT NULL
        THEN ROUND(sig.duration_us / 1000.0, 1) END AS "Duration (ms)",
    CASE WHEN sig.category = 'Auto Grow/Shrink' AND sig.integer_data IS NOT NULL
        THEN ROUND(sig.integer_data * 8.0 / 1024.0, 1) END AS "Growth (MB)",
    sig.severity AS "Severity",
    sig.error_number AS "Error Number",
    sig.text_data AS "Text"
FROM sig
{server_join('sig.server_id')}
WHERE {_DEFAULT_TRACE_WHERE}
ORDER BY sig.event_time_utc DESC
LIMIT 200
"""

_DEFAULT_TRACE_TREND_SQL = f"""
WITH sig AS ({_DEFAULT_TRACE_SIGNIFICANT_SQL})
SELECT
    date_trunc('hour', sig.event_time_utc) AS time,
    sig.category AS metric,
    COUNT(*)::double precision AS value
FROM sig
WHERE {_DEFAULT_TRACE_WHERE}
GROUP BY 1, 2
ORDER BY 1
"""

_DATABASE_VAR_SQL = f"""
SELECT database_name FROM (
    SELECT DISTINCT d.database_name
    FROM {collector('database_size_stats')} AS d
    WHERE {server_filter('d.server_id')}
    UNION
    SELECT DISTINCT dte.database_name
    FROM {collector('default_trace_events')} AS dte
    WHERE {server_filter('dte.server_id')}
) AS dbs
WHERE database_name IS NOT NULL
ORDER BY 1
"""


def system_events():
    """Build the System Events dashboard."""
    reset_id()
    panels: list[dict] = []

    y = subtab(
        panels,
        "Corruption Events",
        0,
        [
            (
                12,
                8,
                lambda x, y, w, h: timeseries(
                    "Bad Pages & Dump Requests",
                    x,
                    y,
                    w,
                    h,
                    [target(_CORRUPTION_BAD_PAGES_SQL)],
                    bars=True,
                ),
            ),
            (
                12,
                8,
                lambda x, y, w, h: timeseries(
                    "Access Violations",
                    x,
                    y,
                    w,
                    h,
                    [target(_CORRUPTION_ACCESS_VIOLATIONS_SQL)],
                    bars=True,
                ),
            ),
        ],
    )

    y = subtab(
        panels,
        "Contention Events",
        y,
        [
            (
                12,
                8,
                lambda x, y, w, h: timeseries(
                    "Non-Yielding Tasks & Latch Warnings",
                    x,
                    y,
                    w,
                    h,
                    [target(_CONTENTION_NONYIELD_SQL)],
                    bars=True,
                ),
            ),
            (
                12,
                8,
                lambda x, y, w, h: timeseries(
                    "SQL CPU vs System CPU",
                    x,
                    y,
                    w,
                    h,
                    [target(_CONTENTION_CPU_SQL)],
                    unit="percent",
                    max_=100,
                    bars=True,
                ),
            ),
            (
                24,
                8,
                lambda x, y, w, h: timeseries(
                    "Sick Spinlocks by Type (top 5)",
                    x,
                    y,
                    w,
                    h,
                    [target(_SICK_SPINLOCKS_SQL)],
                ),
            ),
        ],
    )

    y = subtab(
        panels,
        "Scheduler Issues",
        y,
        [
            (24, 4, stat_grid(_SCHEDULER_ISSUES_STATS, cols=5)),
            (
                24,
                8,
                lambda x, y, w, h: timeseries(
                    "Non-Yielding Scheduler Time",
                    x,
                    y,
                    w,
                    h,
                    [target(_SCHEDULER_ISSUES_TREND_SQL)],
                    unit="ms",
                    bars=True,
                ),
            ),
            (
                24,
                10,
                lambda x, y, w, h: table(
                    "Scheduler Issue Events",
                    x,
                    y,
                    w,
                    h,
                    _SCHEDULER_ISSUES_TABLE_SQL,
                    sort_by=[{"displayName": "Event Time", "desc": True}],
                ),
            ),
        ],
    )

    y = subtab(
        panels,
        "Severe Errors",
        y,
        [
            (
                12,
                8,
                lambda x, y, w, h: timeseries(
                    "Severe Errors per Hour",
                    x,
                    y,
                    w,
                    h,
                    [target(_SEVERE_ERRORS_TREND_SQL)],
                    bars=True,
                ),
            ),
            (
                12,
                8,
                lambda x, y, w, h: table(
                    "Severe Error Log",
                    x,
                    y,
                    w,
                    h,
                    _SEVERE_ERRORS_TABLE_SQL,
                    sort_by=[{"displayName": "Event Time", "desc": True}],
                ),
            ),
        ],
    )

    y = subtab(
        panels,
        "Memory Conditions",
        y,
        [
            (
                24,
                8,
                lambda x, y, w, h: timeseries(
                    "OOM Exceptions (hourly)",
                    x,
                    y,
                    w,
                    h,
                    [target(_MEMORY_CONDITIONS_TREND_SQL)],
                    bars=True,
                ),
            ),
            (
                24,
                9,
                lambda x, y, w, h: table(
                    "Memory Condition Snapshots",
                    x,
                    y,
                    w,
                    h,
                    _MEMORY_CONDITIONS_TABLE_SQL,
                    sort_by=[{"displayName": "Event Time", "desc": True}],
                ),
            ),
        ],
    )

    y = subtab(
        panels,
        "Memory Broker",
        y,
        [
            (
                24,
                8,
                lambda x, y, w, h: timeseries(
                    "Memory Broker - Currently Allocated",
                    x,
                    y,
                    w,
                    h,
                    [target(_MEMORY_BROKER_TREND_SQL)],
                    bars=True,
                ),
            ),
            (
                24,
                8,
                lambda x, y, w, h: table(
                    "Memory Broker Events",
                    x,
                    y,
                    w,
                    h,
                    _MEMORY_BROKER_TABLE_SQL,
                    sort_by=[{"displayName": "Event Time", "desc": True}],
                ),
            ),
        ],
    )

    y = subtab(
        panels,
        "Memory Node OOM",
        y,
        [
            (
                12,
                8,
                lambda x, y, w, h: timeseries(
                    "Memory Node Utilization %",
                    x,
                    y,
                    w,
                    h,
                    [target(_MEMORY_NODE_OOM_UTILIZATION_SQL)],
                    unit="percent",
                    max_=100,
                ),
            ),
            (
                12,
                8,
                lambda x, y, w, h: timeseries(
                    "Memory Node Breakdown",
                    x,
                    y,
                    w,
                    h,
                    [target(_MEMORY_NODE_OOM_BREAKDOWN_SQL)],
                    unit="decgbytes",
                ),
            ),
            (
                12,
                8,
                lambda x, y, w, h: timeseries(
                    "OOM Events (hourly)",
                    x,
                    y,
                    w,
                    h,
                    [target(_MEMORY_NODE_OOM_TREND_SQL)],
                    bars=True,
                ),
            ),
            (12, 8, stat_grid(_MEMORY_NODE_OOM_STATS)),
            (
                24,
                9,
                lambda x, y, w, h: table(
                    "Memory Node OOM Events",
                    x,
                    y,
                    w,
                    h,
                    _MEMORY_NODE_OOM_TABLE_SQL,
                    sort_by=[{"displayName": "Event Time", "desc": True}],
                ),
            ),
        ],
    )

    y = subtab(
        panels,
        "Significant Waits",
        y,
        [
            (24, 4, stat_grid(_SIGNIFICANT_WAITS_STATS, cols=4)),
            (
                24,
                8,
                lambda x, y, w, h: timeseries(
                    "Significant Waits per Hour",
                    x,
                    y,
                    w,
                    h,
                    [target(_SIGNIFICANT_WAITS_TREND_SQL)],
                    bars=True,
                ),
            ),
            (
                24,
                10,
                lambda x, y, w, h: table(
                    "Significant Wait Events",
                    x,
                    y,
                    w,
                    h,
                    _SIGNIFICANT_WAITS_TABLE_SQL,
                    sort_by=[{"displayName": "Event Time", "desc": True}],
                ),
            ),
        ],
    )

    y = subtab(
        panels,
        "CPU Tasks",
        y,
        [
            (24, 4, stat_grid(_CPU_TASKS_STATS, cols=4)),
            (
                12,
                8,
                lambda x, y, w, h: timeseries(
                    "Worker Threads",
                    x,
                    y,
                    w,
                    h,
                    [target(_CPU_TASKS_WORKERS_SQL)],
                    bars=True,
                ),
            ),
            (
                12,
                8,
                lambda x, y, w, h: timeseries(
                    "Pending Tasks & Oldest Pending Wait",
                    x,
                    y,
                    w,
                    h,
                    [target(_CPU_TASKS_PENDING_SQL)],
                    bars=True,
                ),
            ),
            (
                24,
                8,
                lambda x, y, w, h: table(
                    "CPU Task Events",
                    x,
                    y,
                    w,
                    h,
                    _CPU_TASKS_TABLE_SQL,
                    sort_by=[{"displayName": "Event Time", "desc": True}],
                ),
            ),
        ],
    )

    y = subtab(
        panels,
        "I/O Issues",
        y,
        [
            (
                12,
                8,
                lambda x, y, w, h: timeseries(
                    "I/O Latch Timeouts & Long I/Os",
                    x,
                    y,
                    w,
                    h,
                    [target(_IO_ISSUES_TREND_SQL)],
                    bars=True,
                ),
            ),
            (
                12,
                8,
                lambda x, y, w, h: table(
                    "I/O Issue Events",
                    x,
                    y,
                    w,
                    h,
                    _IO_ISSUES_TABLE_SQL,
                    sort_by=[{"displayName": "Event Time", "desc": True}],
                ),
            ),
        ],
    )

    subtab(
        panels,
        "Default Trace",
        y,
        [
            (
                24,
                8,
                lambda x, y, w, h: timeseries(
                    "Events per Hour by Category",
                    x,
                    y,
                    w,
                    h,
                    [target(_DEFAULT_TRACE_TREND_SQL)],
                    stacked=True,
                    bars=True,
                ),
            ),
            (
                24,
                10,
                lambda x, y, w, h: table(
                    "Default Trace Events",
                    x,
                    y,
                    w,
                    h,
                    _DEFAULT_TRACE_TABLE_SQL,
                    sort_by=[{"displayName": "Event Time", "desc": True}],
                    description=(
                        "The always-on server events no DMV captures: file auto-grow/"
                        "shrink stalls, severe ErrorLog writes, schema DDL, security "
                        "audits, Server Memory Change."
                    ),
                ),
            ),
        ],
    )

    database_var = query_var(
        "database",
        "Database",
        _DATABASE_VAR_SQL,
        "Scopes Severe Errors and Default Trace to a database.",
    )

    return dashboard(
        uid("system-events"), "System Events", panels, [server_var(), database_var]
    )
