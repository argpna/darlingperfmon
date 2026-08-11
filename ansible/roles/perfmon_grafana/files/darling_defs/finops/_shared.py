"""Shared fragments for the Darling FinOps dashboards.

Upstream ref: ViewerDataService.FinOps.*.cs, FinOpsTab.*.cs.

A sub-tab with a time-range combo box follows the dashboard range; one whose window upstream
fixes keeps a literal interval, since its thresholds were calibrated against that span.
"""

from .._shared import (
    CAGG_TIME_COL,
    SERVER_REGISTRY,
    UTC_NOW,
    collector,
    fixed,
    n0,
    rollup,
    server_filter,
)

# FinOpsHealthCalculator.ScoreColor and FinOpsTab.xaml's DataTriggers.
HEALTH_SCORE_STEPS = (("red", None), ("yellow", 60), ("green", 80))

PROVISIONING_STATUS_COLORS = {
    "RIGHT SIZED": "green",
    "OVER PROVISIONED": "yellow",
    "UNDER PROVISIONED": "red",
}

SEVERITY_COLORS = {"High": "red", "Medium": "yellow", "Low": "green"}

CONFIDENCE_COLORS = {"High": "green", "Medium": "yellow", "Low": "text"}

# Never counted as idle (GetIdleDatabasesAsync / ServerMetricsSql).
IDLE_DB_EXCLUSIONS = "'master', 'model', 'msdb', 'tempdb', 'PerformanceMonitor'"

# GetIdleDatabasesAsync's daysBack default, shared with ServerMetricsSql's idle count.
IDLE_DAYS = 7

# GetProvisioningTrendAsync's window, and every Recommendations "last 7 days" P95.
TREND_DAYS = 7


def budget_cte(alias: str = "budget") -> str:
    """Per-server monthly budget, upstream's ServerConnection.MonthlyCostUsd.

    0 means no budget set, and every cost column derived from it reads 0.
    """
    return f"""{alias} AS (
    SELECT server_id, COALESCE(monthly_cost_usd, 0)::numeric AS monthly_cost
    FROM {SERVER_REGISTRY}
    WHERE {server_filter()}
)"""


# Hours in the panel's range, standing in for upstream's hoursBack combo value.
RANGE_HOURS = (
    "(EXTRACT(EPOCH FROM ($__timeTo()::timestamp - $__timeFrom()::timestamp)) / 3600.0)"
)


def window_budget(monthly_cost: str = "b.monthly_cost") -> str:
    """The budget prorated to the panel's range, upstream's windowBudget."""
    return f"({monthly_cost} * {RANGE_HOURS} / 730.0)"


def latest_per_server(relation: str, extra: str = "") -> str:
    """Restrict a collector relation to each server's newest snapshot.

    Matched per server, not against one global MAX: $server is multi-select, and a global
    MAX would hide every server but the most recently collected one.
    """
    return (
        "(server_id, collection_time) IN ("
        f"SELECT server_id, MAX(collection_time) FROM {relation} "
        f"WHERE {server_filter()}{extra} GROUP BY server_id)"
    )


def cpu_score(p95: str) -> str:
    """FinOpsHealthCalculator.CpuScore - a continuous curve, no avg-vs-p95 branch."""
    return f"""CASE
        WHEN {p95} <= 70 THEN trunc(100 - {p95} * 50 / 70.0)
        ELSE trunc(GREATEST(0, 50 - ({p95} - 70) * 50 / 30.0))
    END"""


def memory_score(buffer_pool_ratio: str) -> str:
    """FinOpsHealthCalculator.MemoryScore - penalizes under- and over-caching alike."""
    return f"""CASE
        WHEN {buffer_pool_ratio} <= 0.30 THEN 60
        WHEN {buffer_pool_ratio} <= 0.85 THEN 100
        WHEN {buffer_pool_ratio} <= 0.95 THEN trunc(100 - ({buffer_pool_ratio} - 0.85) * 800)
        ELSE trunc(GREATEST(0, 20 - ({buffer_pool_ratio} - 0.95) * 400))
    END"""


def storage_score(free_space_pct: str) -> str:
    """FinOpsHealthCalculator.StorageScore over the size-weighted free percentage."""
    return f"""CASE
        WHEN {free_space_pct} >= 30 THEN 100
        WHEN {free_space_pct} >= 10 THEN trunc(50 + ({free_space_pct} - 10) * 2.5)
        ELSE trunc({free_space_pct} * 5)
    END"""


def overall_score(cpu: str, memory: str, storage: str) -> str:
    """FinOpsHealthCalculator.Overall - 40 % CPU, 30 % memory, 30 % storage."""
    return f"trunc(({cpu}) * 0.40 + ({memory}) * 0.30 + ({storage}) * 0.30)"


def provisioning_status(
    avg_cpu: str, max_cpu: str, p95_cpu: str, mem_ratio: str
) -> str:
    """GetUtilizationEfficiencyAsync's provisioning classification.

    First-match order: OVER wins over UNDER when both hold, as upstream's if/else-if does.
    """
    return f"""CASE
        WHEN {avg_cpu} < 15 AND {max_cpu} < 40 AND COALESCE({mem_ratio}, 0) < 0.5
        THEN 'OVER_PROVISIONED'
        WHEN {p95_cpu} > 85 OR COALESCE({mem_ratio}, 0) > 0.95
        THEN 'UNDER_PROVISIONED'
        ELSE 'RIGHT_SIZED'
    END"""


def status_display(expr: str) -> str:
    """ProvisioningDisplay - the underscore form is internal, the space form is shown."""
    return f"replace({expr}, '_', ' ')"


def classification_explanation(
    status: str, avg_cpu: str, max_cpu: str, p95_cpu: str, bp_pct: str, mem_ratio: str
) -> str:
    """UpdateUtilizationSummary's FinOpsClassificationExplanation, verbatim wording."""
    return f"""CASE {status}
        WHEN 'RIGHT_SIZED' THEN
            'CPU is moderately loaded (avg ' || {fixed(avg_cpu, 1)} || '%, p95 '
            || {fixed(p95_cpu, 1)} || '%) and memory is well-utilized (buffer pool uses '
            || {fixed(bp_pct, 0)} || '% of physical RAM). No action needed.'
        WHEN 'OVER_PROVISIONED' THEN
            'CPU is lightly loaded (avg ' || {fixed(avg_cpu, 1)} || '%, max '
            || {n0(max_cpu)} || '%) and buffer pool uses only ' || {fixed(bp_pct, 0)}
            || '% of physical RAM. This server may have more resources than it needs.'
        WHEN 'UNDER_PROVISIONED' THEN
            CASE WHEN {p95_cpu} > 85
                THEN 'CPU p95 is ' || {fixed(p95_cpu, 1)}
                     || '% (threshold: 85%). This server may need more CPU capacity.'
                ELSE 'Buffer pool uses ' || {fixed(bp_pct, 0)}
                     || '% of physical RAM and memory ratio is ' || {fixed(mem_ratio, 2)}
                     || ' (threshold: 0.95). Memory pressure is high.'
            END
        ELSE ''
    END"""


def idle_db_ctes(
    activity_relation: str, activity_time_col: str, with_details: bool = False
) -> str:
    """The idle-database CTEs shared by Optimization and Recommendations.

    Upstream ref: GetIdleDatabasesAsync. Both callers share this so the two tabs cannot
    disagree about what is idle. with_details adds Optimization's file-count and
    last-execution columns; activity_relation comes from fixed_window_tiers().
    """
    is_raw = activity_time_col == "collection_time"
    exec_col = "delta_execution_count" if is_raw else "execution_count_sum"
    last_exec = "MAX(last_execution_time)" if is_raw else "MAX(last_execution_time_max)"
    sizes = collector("database_size_stats")
    size_extra = ",\n           COUNT(*) AS file_count" if with_details else ""
    activity_extra = (
        f",\n           {last_exec} AS last_execution" if with_details else ""
    )
    idle_extra = (
        ",\n           ds.file_count,\n           a.last_execution"
        if with_details
        else ""
    )
    return f"""db_sizes AS (
    SELECT server_id, database_name,
           SUM(total_size_mb) AS total_size_mb{size_extra}
    FROM {sizes}
    WHERE {server_filter()} AND {latest_per_server(sizes)}
    GROUP BY server_id, database_name
),
db_activity AS (
    SELECT server_id, database_name,
           SUM({exec_col}) AS total_executions{activity_extra}
    FROM {activity_relation}
    WHERE {server_filter()}
      AND {activity_time_col} >= {UTC_NOW} - INTERVAL '{IDLE_DAYS} days'
      AND {exec_col} IS NOT NULL
    GROUP BY server_id, database_name
),
idle_dbs AS (
    SELECT ds.server_id, ds.database_name, ds.total_size_mb{idle_extra}
    FROM db_sizes ds
    LEFT JOIN db_activity a
           ON a.server_id = ds.server_id AND a.database_name = ds.database_name
    WHERE COALESCE(a.total_executions, 0) = 0
      AND ds.database_name NOT IN ({IDLE_DB_EXCLUSIONS})
)"""


def fixed_window_tiers(build, days: int) -> str:
    """Route a fixed `days` window by measured rollup coverage, not the dashboard range.

    tiered() gates on $__timeFrom(), so it only fits a panel whose window IS the range. Here
    the window is fixed and outruns raw retention, so the gate is whether the hourly rollup
    reaches back that far. build(relation, time_col) returns one tier's SELECT; the two
    branches must project identical columns.
    """
    hourly = rollup("query_stats_db", "hourly")
    covers = (
        f"COALESCE((SELECT min({CAGG_TIME_COL}) FROM {hourly})"
        f" <= {UTC_NOW} - INTERVAL '{days} days', false)"
    )
    return (
        f"SELECT * FROM (\n{build(hourly, CAGG_TIME_COL).strip()}\n) AS hourly_tier"
        f" WHERE {covers}"
        "\nUNION ALL\n"
        f"SELECT * FROM (\n{build(collector('query_stats'), 'collection_time').strip()}\n)"
        f" AS raw_tier WHERE NOT {covers}"
    )


def wait_category(wait_type: str = "wait_type") -> str:
    """WaitCategorySummarySql's cost bucketing, verbatim."""
    return f"""CASE
        WHEN {wait_type} IN ('SOS_SCHEDULER_YIELD', 'CXPACKET', 'CXCONSUMER',
                             'CXSYNC_PORT', 'CXSYNC_CONSUMER') THEN 'CPU'
        WHEN {wait_type} ILIKE 'PAGEIOLATCH%'
          OR {wait_type} IN ('WRITELOG', 'IO_COMPLETION', 'ASYNC_IO_COMPLETION')
        THEN 'Storage'
        WHEN {wait_type} IN ('RESOURCE_SEMAPHORE', 'RESOURCE_SEMAPHORE_QUERY_COMPILE',
                             'CMEMTHREAD') THEN 'Memory'
        WHEN {wait_type} = 'ASYNC_NETWORK_IO' THEN 'Network'
        WHEN {wait_type} ILIKE 'LCK_M_%' THEN 'Locks'
        ELSE 'Other'
    END"""


def format_duration(seconds: str) -> str:
    """The maintenance-window finding's FormatDuration (Recommendations), verbatim."""
    return f"""CASE
        WHEN {seconds} >= 3600
        THEN ({seconds} / 3600)::bigint || 'h ' || (({seconds} % 3600) / 60)::bigint
             || 'm ' || ({seconds} % 60)::bigint || 's'
        WHEN {seconds} >= 60
        THEN ({seconds} / 60)::bigint || 'm ' || ({seconds} % 60)::bigint || 's'
        ELSE ({seconds})::bigint || 's'
    END"""
