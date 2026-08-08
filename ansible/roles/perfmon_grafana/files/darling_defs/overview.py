"""Overview dashboard - per-server correlated timeline lanes, a status stat
row, and a rolled-up daily history (absorbs the former Daily Summary dashboard).

Upstream ref: CorrelatedTimelineLanesControl.xaml(.cs), ViewerDataService.Cpu.cs,
.BlockingTrends.cs, .FileIo.cs, .OverviewLanes.cs, PgBaselineProvider.cs,
PerformanceMonitor.Analysis.Baselines.BaselineMath/BaselineBucket, DailySummarySql.RangeSql
/ RangeSqlFor (Darling.Storage), ViewerServerTab.DailySummary.cs, DailyHealthBandCalculator
(PerformanceMonitor.Common).
"""

from ._shared import (
    HEALTH_STATUS_COLORS,
    col_datalink,
    col_hidden,
    col_thresholds,
    col_unit,
    collector,
    dashboard,
    duration_ms,
    fixed,
    flow,
    n0,
    reset_id,
    rollup,
    SERVER_REGISTRY,
    server_filter,
    server_join,
    server_var,
    series_style,
    stat,
    stat_grid,
    status_colors,
    status_history,
    subtab,
    table,
    target,
    thresholds,
    tiered,
    timeseries,
    uid,
    UTC_NOW,
)
from .collection_health import _CADENCE_TABLE, _HEALTH_AGG, _HEALTH_STATUS

# A fresh, stateless read only ever takes SelectBucket's >= CollapseThreshold(10) branch;
# RestoreThreshold(15) only matters for hysteresis across repeated stateful calls.
_FULL_MIN = 10
_HOUR_MIN = 10
_FLAT_MIN = 3


def _baseline_ctes(buckets_sql: str, abs_floor: float) -> str:
    """CTE chain computing one baseline row (mean_val, effective_stddev, sample_count)
    for a single server at $__timeFrom()'s (hour_of_day, day_of_week), collapsing
    Full -> HourOnly -> Flat. buckets_sql must select (hour_of_day, day_of_week,
    mean_val, stddev_val, sample_count, distinct_days) from a `ref` CTE already in scope.

    Upstream ref: PgBaselineProvider.cs, BaselineMath.cs, BaselineBucket.cs.
    """
    return f"""
ref AS (
    SELECT $__timeFrom()::timestamp AS ref_time
),
buckets AS (
    {buckets_sql}
),
target AS (
    SELECT EXTRACT(HOUR FROM ref_time)::int AS hour_of_day,
           EXTRACT(DOW FROM ref_time)::int AS day_of_week
    FROM ref
),
full_bucket AS (
    SELECT b.mean_val, b.stddev_val, b.sample_count
    FROM buckets b, target t
    WHERE b.hour_of_day = t.hour_of_day AND b.day_of_week = t.day_of_week
),
hour_mean AS (
    SELECT
        SUM(b.mean_val * b.sample_count) / NULLIF(SUM(b.sample_count), 0) AS grand_mean,
        SUM(b.sample_count) AS sample_count
    FROM buckets b, target t
    WHERE b.hour_of_day = t.hour_of_day
),
hour_bucket AS (
    SELECT
        hm.grand_mean AS mean_val,
        CASE WHEN hm.sample_count > 1 THEN sqrt(GREATEST(SUM(
            (b.stddev_val * b.stddev_val) * GREATEST(b.sample_count - 1, 0)
            + b.sample_count * (b.mean_val - hm.grand_mean) * (b.mean_val - hm.grand_mean)
        ) / (hm.sample_count - 1), 0)) ELSE 0 END AS stddev_val,
        hm.sample_count
    FROM buckets b, target t, hour_mean hm
    WHERE b.hour_of_day = t.hour_of_day
    GROUP BY hm.grand_mean, hm.sample_count
),
flat_mean AS (
    SELECT
        SUM(b.mean_val * b.sample_count) / NULLIF(SUM(b.sample_count), 0) AS grand_mean,
        SUM(b.sample_count) AS sample_count
    FROM buckets b
),
flat_bucket AS (
    SELECT
        fm.grand_mean AS mean_val,
        CASE WHEN fm.sample_count > 1 THEN sqrt(GREATEST(SUM(
            (b.stddev_val * b.stddev_val) * GREATEST(b.sample_count - 1, 0)
            + b.sample_count * (b.mean_val - fm.grand_mean) * (b.mean_val - fm.grand_mean)
        ) / (fm.sample_count - 1), 0)) ELSE 0 END AS stddev_val,
        fm.sample_count
    FROM buckets b, flat_mean fm
    GROUP BY fm.grand_mean, fm.sample_count
),
selected AS (
    SELECT
        CASE
            WHEN (SELECT sample_count FROM full_bucket) >= {_FULL_MIN} THEN (SELECT mean_val FROM full_bucket)
            WHEN (SELECT sample_count FROM hour_bucket) >= {_HOUR_MIN} THEN (SELECT mean_val FROM hour_bucket)
            WHEN (SELECT sample_count FROM flat_bucket) >= {_FLAT_MIN} THEN (SELECT mean_val FROM flat_bucket)
        END AS mean_val,
        CASE
            WHEN (SELECT sample_count FROM full_bucket) >= {_FULL_MIN} THEN (SELECT stddev_val FROM full_bucket)
            WHEN (SELECT sample_count FROM hour_bucket) >= {_HOUR_MIN} THEN (SELECT stddev_val FROM hour_bucket)
            WHEN (SELECT sample_count FROM flat_bucket) >= {_FLAT_MIN} THEN (SELECT stddev_val FROM flat_bucket)
        END AS stddev_val,
        CASE
            WHEN (SELECT sample_count FROM full_bucket) >= {_FULL_MIN} THEN (SELECT sample_count FROM full_bucket)
            WHEN (SELECT sample_count FROM hour_bucket) >= {_HOUR_MIN} THEN (SELECT sample_count FROM hour_bucket)
            WHEN (SELECT sample_count FROM flat_bucket) >= {_FLAT_MIN} THEN (SELECT sample_count FROM flat_bucket)
        END AS sample_count
),
baseline AS (
    SELECT
        mean_val,
        CASE
            WHEN sample_count IS NULL THEN NULL
            WHEN mean_val = 0 AND COALESCE(stddev_val, 0) <= 0 THEN 0
            ELSE GREATEST(stddev_val, mean_val * 0.01, {abs_floor})
        END AS effective_stddev,
        sample_count
    FROM selected
),
bounds AS (
    SELECT
        mean_val,
        mean_val + 2 * effective_stddev AS upper_val,
        GREATEST(0, mean_val - 2 * effective_stddev) AS lower_val
    FROM baseline
    WHERE sample_count > 0 AND effective_stddev > 0
)"""


def _band_branches_sql(anomaly_value_sql: str | None, min_anomaly: float = 0) -> str:
    """UNION-ALL branches rendering the Baseline Upper/Lower/Mean band as a flat span
    from $__timeFrom() to $__timeTo(), plus an Anomaly marker series when
    anomaly_value_sql is given. anomaly_value_sql must be a bare (time, value) SELECT
    over the metric the baseline was computed from. Requires `bounds` already in scope.
    """
    branches = """
UNION ALL
SELECT $__timeFrom(), 'Baseline Lower', lower_val FROM bounds
UNION ALL
SELECT $__timeTo(), 'Baseline Lower', lower_val FROM bounds
UNION ALL
SELECT $__timeFrom(), 'Baseline Upper', upper_val FROM bounds
UNION ALL
SELECT $__timeTo(), 'Baseline Upper', upper_val FROM bounds
UNION ALL
SELECT $__timeFrom(), 'Baseline Mean', mean_val FROM bounds
UNION ALL
SELECT $__timeTo(), 'Baseline Mean', mean_val FROM bounds"""
    if anomaly_value_sql:
        branches += f"""
UNION ALL
SELECT a.time, 'Anomaly', a.value
FROM ({anomaly_value_sql}) AS a, bounds b
WHERE (a.value > b.upper_val AND a.value >= {min_anomaly}) OR a.value < b.lower_val"""
    return branches


_BAND_OVERRIDES = [
    series_style(
        "Baseline Lower", line=True, line_width=0, hide_legend=True, hide_tooltip=True
    ),
    series_style(
        "Baseline Upper",
        fill_below_to="Baseline Lower",
        line_width=0,
        hide_legend=True,
        hide_tooltip=True,
    ),
    series_style("Baseline Mean", color="#888888", dash=True, line_width=1),
    series_style("Anomaly", color="#E57373", points=True),
]

# CPU lane. sample_time is server-local; de-skewed to naive UTC like cpu.py.
# Upstream ref: CpuUtilizationSql (#1262).
_CPU_DESKEW = """cpu.sample_time
        - INTERVAL '15 minutes'
          * ROUND(EXTRACT(EPOCH FROM (
                MAX(cpu.sample_time) OVER (PARTITION BY cpu.server_id, cpu.collection_time)
                - cpu.collection_time
            )) / 900.0)::double precision"""

_CPU_SAMPLES_SQL = f"""
SELECT
    {_CPU_DESKEW} AS time,
    cpu.sqlserver_cpu_utilization AS sql_cpu,
    cpu.sqlserver_cpu_utilization + COALESCE(cpu.other_process_cpu_utilization, 0) AS total_cpu
FROM {collector('cpu_utilization_stats')} AS cpu
WHERE $__timeFilter(cpu.collection_time)
  AND {server_filter('cpu.server_id')}
"""

# Upstream ref: PgBaselineProvider.GetBaselineQuery(MetricNames.Cpu) - sufficient
# statistics, since one collection can carry up to 60 ring-buffer samples.
_CPU_BUCKETS_SQL = f"""
SELECT
    EXTRACT(HOUR FROM collection_time)::int AS hour_of_day,
    EXTRACT(DOW FROM collection_time)::int AS day_of_week,
    SUM(cpu_sum)::double precision / NULLIF(SUM(cpu_count), 0) AS mean_val,
    sqrt(GREATEST(
        (SUM(cpu_sumsq) - POWER(SUM(cpu_sum), 2)::double precision / NULLIF(SUM(cpu_count), 0))
        / NULLIF(SUM(cpu_count) - 1, 0), 0)) AS stddev_val,
    SUM(cpu_count) AS sample_count,
    COUNT(DISTINCT collection_time::date) AS distinct_days
FROM collect.cpu_utilization_baseline, ref
WHERE {server_filter('server_id')}
AND   collection_time >= ref.ref_time - INTERVAL '30 days'
AND   collection_time <  ref.ref_time
GROUP BY 1, 2
"""


def _cpu_lane_sql() -> str:
    ctes = _baseline_ctes(_CPU_BUCKETS_SQL, abs_floor=5.0)
    bands = _band_branches_sql(
        "SELECT time, sql_cpu AS value FROM samples", min_anomaly=10
    )
    return f"""
WITH samples AS ({_CPU_SAMPLES_SQL}),
{ctes}
SELECT time, 'SQL CPU' AS metric, sql_cpu AS value FROM samples
UNION ALL
SELECT time, 'Total CPU', total_cpu FROM samples
{bands}
ORDER BY 1
"""


# Wait ms/sec lane
# Upstream ref: GetTotalWaitTrendAsync (ViewerDataService.OverviewLanes.cs).
_WAIT_LANE_DATA_SQL = f"""
WITH per_collection AS (
    SELECT
        collection_time,
        SUM(delta_wait_time_ms) AS total_delta_ms,
        EXTRACT(EPOCH FROM (date_trunc('second', collection_time)
            - date_trunc('second', LAG(collection_time) OVER (ORDER BY collection_time)))) AS interval_seconds
    FROM {collector('wait_stats')}
    WHERE $__timeFilter(collection_time)
      AND {server_filter('server_id')}
    GROUP BY collection_time
)
SELECT
    collection_time AS time,
    CASE WHEN interval_seconds > 0 THEN total_delta_ms::double precision / interval_seconds ELSE 0 END AS value
FROM per_collection
"""

# Upstream ref: PgBaselineProvider.GetBaselineQuery(MetricNames.WaitMsPerSec). The
# restart-exclusion LAG applies to the rate, not the raw delta.
_WAIT_BUCKETS_SQL = f"""
WITH per_collection AS (
    SELECT
        collection_time,
        total_wait_ms::double precision AS total_wait_ms,
        EXTRACT(EPOCH FROM (date_trunc('second', collection_time)
            - date_trunc('second', LAG(collection_time) OVER (ORDER BY collection_time)))) AS interval_sec
    FROM collect.wait_stats_baseline, ref
    WHERE {server_filter('server_id')}
    AND   collection_time >= ref.ref_time - INTERVAL '30 days'
    AND   collection_time <  ref.ref_time
),
with_rate AS (
    SELECT collection_time,
           CASE WHEN interval_sec > 0 THEN total_wait_ms / interval_sec ELSE 0 END AS ms_per_sec
    FROM per_collection
    WHERE interval_sec IS NOT NULL
),
with_lag AS (
    SELECT collection_time, ms_per_sec,
           COALESCE(LAG(ms_per_sec) OVER (ORDER BY collection_time), 0) AS prior_ms_per_sec
    FROM with_rate
)
SELECT
    EXTRACT(HOUR FROM collection_time)::int AS hour_of_day,
    EXTRACT(DOW FROM collection_time)::int AS day_of_week,
    AVG(ms_per_sec) AS mean_val,
    COALESCE(STDDEV_SAMP(ms_per_sec), 0) AS stddev_val,
    COUNT(*) AS sample_count,
    COUNT(DISTINCT collection_time::date) AS distinct_days
FROM with_lag
WHERE NOT (ms_per_sec = 0 AND prior_ms_per_sec > 100)
GROUP BY 1, 2
"""


def _wait_lane_sql() -> str:
    ctes = _baseline_ctes(_WAIT_BUCKETS_SQL, abs_floor=0.0)
    bands = _band_branches_sql("SELECT time, value FROM samples", min_anomaly=100)
    return f"""
WITH samples AS ({_WAIT_LANE_DATA_SQL}),
{ctes}
SELECT time, 'Wait ms/sec' AS metric, value FROM samples
{bands}
ORDER BY 1
"""


# Blocking & Deadlocking lane. XE preferred, DMV fallback only when XE is empty for the
# window - same convention as blocking.py's Trends sub-tab.
# Upstream ref: BlockingTrendSql/DeadlockTrendSql.
_BLOCKING_DATA_SQL = f"""
WITH bpr AS (
    SELECT date_trunc('minute', event_time) AS t, COUNT(*) AS c
    FROM {collector('blocked_process_reports')}
    WHERE $__timeFilter(collection_time)
      AND {server_filter('server_id')}
    GROUP BY 1
),
dmv AS (
    SELECT date_trunc('minute', event_time) AS t, COUNT(*) AS c
    FROM {collector('dmv_blocking_snapshots')}
    WHERE $__timeFilter(collection_time)
      AND {server_filter('server_id')}
      AND NOT EXISTS (SELECT 1 FROM bpr)
    GROUP BY 1
)
SELECT t AS time, c AS value FROM bpr
UNION ALL
SELECT t, c FROM dmv
"""

_DEADLOCK_DATA_SQL = f"""
SELECT date_trunc('minute', deadlock_time) AS time, COUNT(*) AS value
FROM {collector('deadlocks')}
WHERE $__timeFilter(collection_time)
  AND {server_filter('server_id')}
GROUP BY 1
"""

# Upstream ref: PgBaselineProvider.GetBaselineQuery(MetricNames.BlockingPerMinute) - one
# band shared by both series, matching UpdateBlockingLane.
_BLOCKING_BUCKETS_SQL = f"""
WITH per_minute AS (
    SELECT DATE_TRUNC('minute', collection_time) AS minute_bucket,
           SUM(event_count)::double precision AS event_count
    FROM collect.blocked_process_baseline, ref
    WHERE {server_filter('server_id')}
    AND   collection_time >= ref.ref_time - INTERVAL '30 days'
    AND   collection_time <  ref.ref_time
    GROUP BY minute_bucket
)
SELECT
    EXTRACT(HOUR FROM minute_bucket)::int AS hour_of_day,
    EXTRACT(DOW FROM minute_bucket)::int AS day_of_week,
    AVG(event_count) AS mean_val,
    COALESCE(STDDEV_SAMP(event_count), 0) AS stddev_val,
    COUNT(*) AS sample_count,
    COUNT(DISTINCT minute_bucket::date) AS distinct_days
FROM per_minute
GROUP BY 1, 2
"""


def _blocking_lane_sql() -> str:
    ctes = _baseline_ctes(_BLOCKING_BUCKETS_SQL, abs_floor=0.0)
    # No anomaly markers on this lane - UpdateBlockingLane draws the band only.
    bands = _band_branches_sql(None)
    return f"""
WITH blocking_data AS ({_BLOCKING_DATA_SQL}),
deadlock_data AS ({_DEADLOCK_DATA_SQL}),
{ctes}
SELECT time, 'Blocking' AS metric, value FROM blocking_data
UNION ALL
SELECT time, 'Deadlocks', value FROM deadlock_data
{bands}
ORDER BY 1
"""


# Buffer Pool MB lane (no baseline). Plots buffer_pool_mb only, not the full
# MemoryTrendPoint shape. Upstream ref: GetMemoryTrendAsync (ViewerDataService.OverviewLanes.cs).
_MEMORY_LANE_SQL = f"""
SELECT
    collection_time AS time,
    'Buffer Pool MB' AS metric,
    CAST(buffer_pool_mb AS double precision) AS value
FROM {collector('memory_stats')}
WHERE $__timeFilter(collection_time)
  AND {server_filter('server_id')}
ORDER BY 1
"""


# File I/O latency lane, averaged across the top-10 busiest files per collection_time.
# Upstream ref: GetFileIoLatencyTrendAsync (ViewerDataService.FileIo.cs).
_FILE_IO_DATA_SQL = f"""
WITH top_files AS (
    SELECT database_name, file_name
    FROM {collector('file_io_stats')}
    WHERE $__timeFilter(collection_time)
      AND {server_filter('server_id')}
      AND (delta_reads > 0 OR delta_writes > 0)
    GROUP BY database_name, file_name
    ORDER BY SUM(delta_reads + delta_writes) DESC
    LIMIT 10
),
per_file AS (
    SELECT
        f.collection_time,
        CASE WHEN SUM(f.delta_reads) > 0
             THEN SUM(f.delta_stall_read_ms::double precision) / SUM(f.delta_reads)
             ELSE 0 END AS avg_read_latency_ms
    FROM {collector('file_io_stats')} AS f
    JOIN top_files tf ON tf.database_name = f.database_name AND tf.file_name = f.file_name
    WHERE $__timeFilter(f.collection_time)
      AND {server_filter('f.server_id')}
    GROUP BY f.collection_time, f.database_name, f.file_name
)
SELECT collection_time AS time, AVG(avg_read_latency_ms) AS value
FROM per_file
GROUP BY collection_time
"""

# Upstream ref: PgBaselineProvider.GetBaselineQuery(MetricNames.IoLatency) - sufficient
# statistics over the per-file ratio, same shape as the CPU baseline.
_FILE_IO_BUCKETS_SQL = f"""
SELECT
    EXTRACT(HOUR FROM collection_time)::int AS hour_of_day,
    EXTRACT(DOW FROM collection_time)::int AS day_of_week,
    SUM(ratio_sum) / NULLIF(SUM(ratio_count), 0) AS mean_val,
    sqrt(GREATEST(
        (SUM(ratio_sumsq) - POWER(SUM(ratio_sum), 2) / NULLIF(SUM(ratio_count), 0))
        / NULLIF(SUM(ratio_count) - 1, 0), 0)) AS stddev_val,
    SUM(row_count) AS sample_count,
    COUNT(DISTINCT collection_time::date) AS distinct_days
FROM collect.file_io_baseline, ref
WHERE {server_filter('server_id')}
AND   collection_time >= ref.ref_time - INTERVAL '30 days'
AND   collection_time <  ref.ref_time
GROUP BY 1, 2
"""


def _file_io_lane_sql() -> str:
    ctes = _baseline_ctes(_FILE_IO_BUCKETS_SQL, abs_floor=2.5)
    bands = _band_branches_sql("SELECT time, value FROM samples", min_anomaly=2)
    return f"""
WITH samples AS ({_FILE_IO_DATA_SQL}),
{ctes}
SELECT time, 'I/O ms' AS metric, value FROM samples
{bands}
ORDER BY 1
"""

_ANOMALY_PAD = "5 minutes"


def _anomaly_islands_sql(
    samples_sql: str, value_col: str, buckets_sql: str, abs_floor: float, min_anomaly: float
) -> str:
    """Group one lane's flagged anomaly samples into contiguous events, one row per event:
    (start_time, end_time, sample_count, peak_value). A non-anomalous sample breaks the
    run, so a run of consecutive flagged samples with no unflagged sample between them is
    one event regardless of collection cadence.

    end_time - start_time is NOT exposed as a "duration" - at this collector's cadence
    almost every event is exactly one flagged sample (start_time = end_time), so a
    from/to span reads as "0ms" for the overwhelming majority of rows: not just
    uninformative but actively misleading, since the metric really did spike, we just
    can't resolve how long for between collections. sample_count (how many consecutive
    collections stayed flagged) is the honest version of the same "blip vs sustained"
    signal.
    """
    ctes = _baseline_ctes(buckets_sql, abs_floor)
    return f"""
WITH samples AS ({samples_sql}),
{ctes},
flagged AS (
    SELECT s.time, s.{value_col} AS value,
        CASE WHEN (s.{value_col} > b.upper_val AND s.{value_col} >= {min_anomaly})
                  OR s.{value_col} < b.lower_val
             THEN 1 ELSE 0 END AS is_anomaly
    FROM samples s, bounds b
),
islands AS (
    SELECT time, value, is_anomaly,
        SUM(CASE WHEN is_anomaly = 0 THEN 1 ELSE 0 END) OVER (ORDER BY time) AS grp
    FROM flagged
)
SELECT MIN(time) AS start_time, MAX(time) AS end_time, COUNT(*) AS sample_count,
       MAX(value) AS peak_value
FROM islands
WHERE is_anomaly = 1
GROUP BY grp
"""


def _anomaly_row_sql(metric_label: str, islands_sql: str, peak_expr: str) -> str:
    """One UNION branch of the Anomalies table. peak_value is MAX(value) over the event -
    the worst reading for the common "spike" case (CPU/wait/latency running high); a "dip"
    event (value < lower_val) instead shows its least-low point, a minor approximation
    accepted for now since spikes dominate this data set. target_url carries only the
    padded from/to (the part that has to be computed in SQL) - $server is filled in by
    Grafana's own client-side substitution on the link template, like every other link here.
    """
    return f"""
SELECT
    '{metric_label}' AS "Metric",
    start_time AS "Start Time",
    sample_count AS "Samples",
    {peak_expr} AS "Peak Value",
    'from=' || (EXTRACT(EPOCH FROM (start_time - INTERVAL '{_ANOMALY_PAD}')) * 1000)::bigint
        || '&to=' || (EXTRACT(EPOCH FROM (end_time + INTERVAL '{_ANOMALY_PAD}')) * 1000)::bigint
        AS "target_url"
FROM ({islands_sql}) AS events
"""


_ANOMALIES_SQL = f"""
{_anomaly_row_sql(
    "SQL CPU",
    _anomaly_islands_sql(_CPU_SAMPLES_SQL, "sql_cpu", _CPU_BUCKETS_SQL, 5.0, 10),
    fixed("peak_value", 1) + " || '%'",
)}
UNION ALL
{_anomaly_row_sql(
    "Wait ms/sec",
    _anomaly_islands_sql(_WAIT_LANE_DATA_SQL, "value", _WAIT_BUCKETS_SQL, 0.0, 100),
    fixed("peak_value", 0) + " || ' ms/sec'",
)}
UNION ALL
{_anomaly_row_sql(
    "I/O Latency",
    _anomaly_islands_sql(_FILE_IO_DATA_SQL, "value", _FILE_IO_BUCKETS_SQL, 2.5, 2),
    fixed("peak_value", 1) + " || ' ms'",
)}
ORDER BY "Start Time" DESC
"""

_ANOMALY_DRILL = col_datalink(
    "Start Time",
    "Show Active Queries at This Time",
    "/d/darling-queries?${__data.fields.target_url}&var-server=$server",
)


# Stat row. Upstream ref: server_inventory.py's _VERSION_DISPLAY/_UPTIME for the same
# ServerPropertyRow computed columns, re-derived here scoped to one server instead of the
# whole fleet.
_EDITION_SQL = f"""
SELECT sp.edition AS "Edition"
FROM {collector('server_properties')} AS sp
WHERE {server_filter('sp.server_id')}
ORDER BY sp.collection_time DESC
LIMIT 1
"""

_VERSION_SQL = f"""
SELECT CASE WHEN COALESCE(sp.product_update_level, '') <> ''
         THEN sp.product_version || ' (' || sp.product_update_level || ')'
         ELSE sp.product_version END AS "Version"
FROM {collector('server_properties')} AS sp
WHERE {server_filter('sp.server_id')}
ORDER BY sp.collection_time DESC
LIMIT 1
"""

_UPTIME_SQL = f"""
SELECT CASE WHEN sqlserver_start_time IS NULL THEN 'N/A'
    ELSE (EXTRACT(EPOCH FROM (LOCALTIMESTAMP - sqlserver_start_time)) / 86400)::int || 'd '
         || ((EXTRACT(EPOCH FROM (LOCALTIMESTAMP - sqlserver_start_time))::bigint
              % 86400) / 3600) || 'h'
    END AS "Uptime"
FROM {collector('server_properties')}
WHERE {server_filter()}
ORDER BY collection_time DESC
LIMIT 1
"""

_CURRENT_CPU_SQL = f"""
SELECT sqlserver_cpu_utilization + COALESCE(other_process_cpu_utilization, 0) AS v
FROM {collector('cpu_utilization_stats')}
WHERE {server_filter()}
ORDER BY collection_time DESC
LIMIT 1
"""

# A short trailing window, not $__timeFilter - this is a "right now" snapshot tile like
# Collection Health's stat row, independent of the dashboard's selected time range.
_CURRENT_WAIT_SQL = f"""
WITH per_collection AS (
    SELECT
        collection_time,
        SUM(delta_wait_time_ms) AS total_delta_ms,
        EXTRACT(EPOCH FROM (date_trunc('second', collection_time)
            - date_trunc('second', LAG(collection_time) OVER (ORDER BY collection_time))))
            AS interval_seconds
    FROM {collector('wait_stats')}
    WHERE collection_time >= {UTC_NOW} - INTERVAL '1 hour'
      AND {server_filter()}
    GROUP BY collection_time
)
SELECT CASE WHEN interval_seconds > 0
            THEN total_delta_ms::double precision / interval_seconds ELSE 0 END AS v
FROM per_collection
ORDER BY collection_time DESC
LIMIT 1
"""

# XE preferred, DMV fallback - same convention as _BLOCKING_DATA_SQL above.
_BLOCKING_DEADLOCKS_TODAY_SQL = f"""
WITH bpr AS (
    SELECT COUNT(*) AS c
    FROM {collector('blocked_process_reports')}
    WHERE collection_time >= date_trunc('day', {UTC_NOW}) AND {server_filter()}
),
dmv AS (
    SELECT COUNT(*) AS c
    FROM {collector('dmv_blocking_snapshots')}
    WHERE collection_time >= date_trunc('day', {UTC_NOW}) AND {server_filter()}
),
deadlocks AS (
    SELECT COUNT(*) AS c
    FROM {collector('deadlocks')}
    WHERE collection_time >= date_trunc('day', {UTC_NOW}) AND {server_filter()}
)
SELECT COALESCE(NULLIF((SELECT c FROM bpr), 0), (SELECT c FROM dmv), 0)
       + (SELECT c FROM deadlocks) AS v
"""

_BUFFER_POOL_PCT_SQL = f"""
SELECT CASE WHEN total_server_memory_mb > 0
            THEN buffer_pool_mb::double precision / total_server_memory_mb * 100 END AS v
FROM {collector('memory_stats')}
WHERE {server_filter()}
ORDER BY collection_time DESC
LIMIT 1
"""

# Reuses collection_health.py's 7-day collector banding (same pattern fleet.py uses for its
# fleet-wide Collectors Failing tile) rather than re-deriving the classifier here.
_FAILING_COLLECTORS_SQL = f"""
WITH agg AS ({_HEALTH_AGG}),
cadence(collector_name, frequency_minutes) AS (VALUES
    {_CADENCE_TABLE}
),
a AS (
    SELECT
        agg.*,
        COALESCE(c.frequency_minutes, 0) AS frequency_minutes,
        CASE WHEN agg.total_runs > 0
             THEN agg.error_count::double precision / agg.total_runs * 100
             ELSE 0 END AS failure_rate,
        COALESCE(EXTRACT(EPOCH FROM ((now() AT TIME ZONE 'UTC') - agg.last_success_time))
                 / 3600.0, 999) AS hours_since_success
    FROM agg
    LEFT JOIN cadence c ON c.collector_name = agg.collector_name
)
SELECT COUNT(*) FILTER (WHERE status = 'FAILING') AS v
FROM (SELECT {_HEALTH_STATUS} AS status FROM a) AS x
"""

_STAT_ROW = [
    {
        "title": "Edition",
        "sql": _EDITION_SQL,
        "th": thresholds(("text", None)),
        "fields": "/.*/",
    },
    {
        "title": "Version",
        "sql": _VERSION_SQL,
        "th": thresholds(("text", None)),
        "fields": "/.*/",
    },
    {
        "title": "Uptime",
        "sql": _UPTIME_SQL,
        "th": thresholds(("text", None)),
        "fields": "/.*/",
    },
    {
        "title": "CPU % (Now)",
        "sql": _CURRENT_CPU_SQL,
        "unit": "percent",
        "th": thresholds(("green", None), ("yellow", 80), ("red", 95)),
    },
    {
        "title": "Wait ms/sec (Now)",
        "sql": _CURRENT_WAIT_SQL,
        "th": thresholds(("text", None)),
    },
    {
        "title": "Blocking & Deadlocks (Today)",
        "sql": _BLOCKING_DEADLOCKS_TODAY_SQL,
        "th": thresholds(("green", None), ("yellow", 1), ("red", 5)),
    },
    {
        "title": "Buffer Pool % (Now)",
        "sql": _BUFFER_POOL_PCT_SQL,
        "unit": "percent",
        "th": thresholds(("text", None)),
    },
    {
        "title": "Failing Collectors",
        "sql": _FAILING_COLLECTORS_SQL,
        "th": thresholds(("green", None), ("red", 1)),
    },
]

_HIGH_CPU_CRITICAL = 6
_HIGH_CPU_WARNING = 1
_BLOCKING_CRITICAL = 11
_BLOCKING_WARNING = 1
_ALERT_WARNING = 1

_DAILY_STATES = [(1, "Healthy", "green"), (2, "Warning", "yellow"), (3, "Critical", "red")]

# Upstream ref: DailyHealthBandCalculator.Classify - severity is first-match-wins.
_BAND_LEVEL = f"""CASE
        WHEN d.deadlock_count > 0
          OR d.collection_errors > 0
          OR d.memory_critical_events > 0
          OR d.high_cpu_events >= {_HIGH_CPU_CRITICAL}
          OR d.blocking_events >= {_BLOCKING_CRITICAL}
        THEN 3
        WHEN d.high_cpu_events >= {_HIGH_CPU_WARNING}
          OR d.blocking_events >= {_BLOCKING_WARNING}
          OR d.memory_pressure_events > 0
          OR d.alert_count >= {_ALERT_WARNING}
        THEN 2
        ELSE 1
    END"""

# DailyHealthBandCalculator.Label, off the same level so the two cannot disagree.
_BAND_LABEL = (
    f"CASE {_BAND_LEVEL} "
    + " ".join(f"WHEN {value} THEN '{text}'" for value, text, _ in _DAILY_STATES)
    + " END"
)


def _count_line(expr: str, singular: str, plural: str) -> str:
    """One BuildSignalLines entry: NULL when the signal is zero, so concat_ws drops it."""
    return f"""CASE WHEN {expr} > 0
        THEN {n0(expr)} || ' ' || CASE WHEN {expr} = 1 THEN '{singular}' ELSE '{plural}' END
    END"""


# The blocking line carries the day's peak block duration, as the day-detail panel does.
_BLOCKING_LINE = f"""CASE WHEN d.blocking_events > 0
        THEN {n0('d.blocking_events')}
             || CASE WHEN d.blocking_events = 1 THEN ' blocking event' ELSE ' blocking events' END
             || CASE WHEN d.peak_block_wait_ms > 0
                     THEN ' (peak block ' || {duration_ms('d.peak_block_wait_ms')} || ')'
                     ELSE '' END
    END"""

# MemoryPressureEvents is the full count; the severe subset is listed on its own line, so
# only the remainder goes here (upstream avoids the same double-count).
_DAILY_REASONS = f"""COALESCE(NULLIF(concat_ws(', ',
        {_count_line('d.deadlock_count', 'deadlock', 'deadlocks')},
        {_count_line('d.collection_errors', 'collection error', 'collection errors')},
        {_count_line('d.high_cpu_events', 'high-CPU sample', 'high-CPU samples')},
        {_BLOCKING_LINE},
        {_count_line('d.memory_critical_events',
                     'severe memory-pressure event', 'severe memory-pressure events')},
        {_count_line('GREATEST(0, d.memory_pressure_events - d.memory_critical_events)',
                     'memory-pressure event', 'memory-pressure events')},
        {_count_line('d.alert_count', 'alert', 'alerts')}
    ), ''), 'No issues detected.')"""


def _daily_queries_cte(relation: str, time_col: str) -> str:
    """The one CTE that reads a table with a raw horizon shorter than its rollups reach.

    COUNT(DISTINCT query_hash) is exact over a CAGG because query_hash is one of its GROUP BY
    columns. Upstream ref: DailySummarySql.QueriesCteForCagg (#1661).
    """
    return f"""
    SELECT server_id, date_trunc('day', {time_col}) AS d,
           COUNT(DISTINCT query_hash) AS c
    FROM {relation}
    WHERE $__timeFilter({time_col}) AND {server_filter()}
    GROUP BY 1, 2
"""


def _daily_range_sql(queries_cte: str) -> str:
    """The whole daily aggregate for one retention tier, one row per day for $server."""
    return f"""
WITH wait_per_type AS (
    SELECT ws.server_id, date_trunc('day', ws.collection_time) AS d, ws.wait_type,
           SUM(ws.delta_wait_time_ms) AS ms
    FROM {collector('wait_stats')} AS ws
    WHERE $__timeFilter(ws.collection_time)
      AND {server_filter('ws.server_id')}
      AND ws.delta_wait_time_ms > 0
    GROUP BY 1, 2, 3
),
wait_totals AS (
    SELECT server_id, d, SUM(ms) / 1000.0 AS total_wait_sec
    FROM wait_per_type GROUP BY 1, 2
),
wait_top AS (
    /* Per-day top wait type = the wait with the most delta time that day. */
    SELECT DISTINCT ON (server_id, d) server_id, d, wait_type AS top_wait_type
    FROM wait_per_type ORDER BY server_id, d, ms DESC
),
waits AS (
    SELECT t.server_id, t.d, t.total_wait_sec, tp.top_wait_type
    FROM wait_totals t
    LEFT JOIN wait_top tp ON tp.server_id = t.server_id AND tp.d = t.d
),
queries AS ({queries_cte}),
deadlocks AS (
    SELECT server_id, date_trunc('day', collection_time) AS d, COUNT(*) AS c
    FROM {collector('deadlocks')}
    WHERE $__timeFilter(collection_time) AND {server_filter()}
    GROUP BY 1, 2
),
bpr AS (
    SELECT server_id, date_trunc('day', collection_time) AS d,
           COUNT(*) AS c, MAX(wait_time_ms) AS max_wait_ms
    FROM {collector('blocked_process_reports')}
    WHERE $__timeFilter(collection_time) AND {server_filter()}
    GROUP BY 1, 2
),
dmv AS (
    SELECT server_id, date_trunc('day', collection_time) AS d,
           COUNT(*) AS c, MAX(wait_time_ms) AS max_wait_ms
    FROM {collector('dmv_blocking_snapshots')}
    WHERE $__timeFilter(collection_time) AND {server_filter()}
    GROUP BY 1, 2
),
cpu AS (
    /* Total host CPU = SQL + other-process (NULL on Linux -> 0), matching the alert engine;
       sustained >= 80 samples drive the day's band. */
    SELECT server_id, date_trunc('day', collection_time) AS d,
           COUNT(*) FILTER (
               WHERE (sqlserver_cpu_utilization
                      + COALESCE(other_process_cpu_utilization, 0)) >= 80) AS c
    FROM {collector('cpu_utilization_stats')}
    WHERE $__timeFilter(collection_time) AND {server_filter()}
    GROUP BY 1, 2
),
coll AS (
    /* Any run marks the day as collected, so a quiet monitored day still bands Healthy. */
    SELECT server_id, date_trunc('day', collection_time) AS d,
           COUNT(*) AS runs,
           COUNT(*) FILTER (WHERE status = 'ERROR') AS errs
    FROM {collector('collection_log')}
    WHERE $__timeFilter(collection_time) AND {server_filter()}
    GROUP BY 1, 2
),
mem AS (
    SELECT server_id, date_trunc('day', collection_time) AS d,
           COUNT(*) FILTER (
               WHERE memory_indicators_process >= 2
                  OR memory_indicators_system >= 2) AS pressure,
           COUNT(*) FILTER (WHERE memory_indicators_process >= 3) AS critical
    FROM {collector('memory_pressure_events')}
    WHERE $__timeFilter(collection_time) AND {server_filter()}
    GROUP BY 1, 2
),
alerts AS (
    /* Actionable alerts only: no dismissed rows, no resolution notices. The suffix list
       mirrors AlertMetricClassifier.IsResolution and must match it exactly - widening one
       without the other counts recoveries as actionable alerts. */
    SELECT server_id, date_trunc('day', alert_time) AS d, COUNT(*) AS c
    FROM config.config_alert_log
    WHERE $__timeFilter(alert_time)
      AND {server_filter()}
      AND dismissed = FALSE
      AND metric_name NOT LIKE '%Cleared%'
      AND metric_name NOT LIKE '%Resolved%'
      AND metric_name NOT LIKE '%Restored%'
      AND metric_name NOT LIKE '%Resumed%'
      AND metric_name NOT LIKE '%Restarted%'
      AND metric_name NOT LIKE '%Recovered%'
      AND metric_name NOT LIKE '%Reconnected%'
    GROUP BY 1, 2
),
day_spine AS (
    SELECT server_id, d FROM waits
    UNION SELECT server_id, d FROM queries
    UNION SELECT server_id, d FROM deadlocks
    UNION SELECT server_id, d FROM bpr
    UNION SELECT server_id, d FROM dmv
    UNION SELECT server_id, d FROM cpu
    UNION SELECT server_id, d FROM coll
    UNION SELECT server_id, d FROM mem
    UNION SELECT server_id, d FROM alerts
)
SELECT
    s.d AS day,
    s.server_id,
    COALESCE(w.total_wait_sec, 0) AS total_wait_sec,
    w.top_wait_type,
    COALESCE(q.c, 0) AS unique_queries,
    COALESCE(dl.c, 0) AS deadlock_count,
    COALESCE(NULLIF(b.c, 0), dm.c, 0) AS blocking_events,
    COALESCE(cp.c, 0) AS high_cpu_events,
    COALESCE(cl.errs, 0) AS collection_errors,
    COALESCE(m.pressure, 0) AS memory_pressure_events,
    COALESCE(m.critical, 0) AS memory_critical_events,
    COALESCE(al.c, 0) AS alert_count,
    /* Peak block wait from the SAME source the count came from (BPR preferred, DMV-snapshot
       fallback), so the blocking reason reconciles with the count. */
    COALESCE(CASE WHEN COALESCE(b.c, 0) > 0 THEN b.max_wait_ms ELSE dm.max_wait_ms END, 0)
        AS peak_block_wait_ms
FROM day_spine s
LEFT JOIN waits w ON w.server_id = s.server_id AND w.d = s.d
LEFT JOIN queries q ON q.server_id = s.server_id AND q.d = s.d
LEFT JOIN deadlocks dl ON dl.server_id = s.server_id AND dl.d = s.d
LEFT JOIN bpr b ON b.server_id = s.server_id AND b.d = s.d
LEFT JOIN dmv dm ON dm.server_id = s.server_id AND dm.d = s.d
LEFT JOIN cpu cp ON cp.server_id = s.server_id AND cp.d = s.d
LEFT JOIN coll cl ON cl.server_id = s.server_id AND cl.d = s.d
LEFT JOIN mem m ON m.server_id = s.server_id AND m.d = s.d
LEFT JOIN alerts al ON al.server_id = s.server_id AND al.d = s.d
"""


# Only the queries CTE changes per tier, exactly as RangeSqlFor swaps it.
_DAILY = tiered(
    {
        "raw": _daily_range_sql(_daily_queries_cte(collector("query_stats"), "collection_time")),
        "hourly": _daily_range_sql(
            _daily_queries_cte(rollup("query_stats", "hourly"), "bucket")
        ),
        "daily": _daily_range_sql(
            _daily_queries_cte(rollup("query_stats", "daily"), "bucket")
        ),
    },
    base="query_stats",
)

# Fixed panel-level time override for the History section - independent of Overview's
# now-3h dashboard default, so the calendar/table always show a real multi-day window.
_HISTORY_WINDOW = "30d"

_CALENDAR_SQL = f"""
WITH d AS ({_DAILY}),
day_range AS (
    SELECT generate_series(
        date_trunc('day', $__timeFrom()::timestamptz),
        date_trunc('day', $__timeTo()::timestamptz),
        INTERVAL '1 day'
    ) AS day
),
spine AS (
    SELECT dr.day, srv.server_id, srv.name
    FROM day_range AS dr
    CROSS JOIN {SERVER_REGISTRY} AS srv
    WHERE srv.is_enabled AND {server_filter('srv.server_id')}
)
SELECT
    spine.day AS time,
    spine.name AS metric,
    CASE WHEN d.day IS NULL THEN NULL ELSE ({_BAND_LEVEL}) END AS band,
    (EXTRACT(EPOCH FROM (spine.day + INTERVAL '1 day')) * 1000)::bigint AS day_end
FROM spine
LEFT JOIN d ON d.server_id = spine.server_id AND d.day = spine.day
ORDER BY 1
"""

_CALENDAR_OVERRIDES = [
    {
        "matcher": {"id": "byName", "options": "band"},
        "properties": [{"id": "displayName", "value": "${__field.labels.metric}"}],
    },
    {
        "matcher": {"id": "byName", "options": "day_end"},
        "properties": [
            {
                "id": "custom.hideFrom",
                "value": {"legend": True, "tooltip": True, "viz": True},
            }
        ],
    },
]

_CALENDAR_DRILL = [
    {
        "title": "Show Active Queries for This Day",
        "url": (
            "/d/darling-queries?from=${__value.time}&to=${__data.fields.day_end}"
            "&var-server=$server"
        ),
        "targetBlank": False,
    }
]

_DAILY_DETAIL_SQL = f"""
WITH d AS ({_DAILY})
SELECT
    d.day AS "Day",
    srv.name AS "Server",
    {_BAND_LABEL} AS "Health",
    {_DAILY_REASONS} AS "Why",
    COALESCE(NULLIF(d.top_wait_type, ''), 'none') AS "Top Wait",
    d.total_wait_sec AS "Total Wait",
    d.unique_queries AS "Unique Queries",
    d.high_cpu_events AS "High-CPU Samples",
    d.deadlock_count AS "Deadlocks",
    d.blocking_events AS "Blocking Events",
    d.peak_block_wait_ms AS "Peak Block",
    d.memory_pressure_events AS "Memory Pressure",
    d.memory_critical_events AS "Severe Memory",
    d.collection_errors AS "Collection Errors",
    d.alert_count AS "Alerts"
FROM d
{server_join('d.server_id')}
ORDER BY d.day DESC, srv.name
"""


def overview():
    """Build the Overview dashboard: status stat row, five correlated timeline lanes, and
    a daily history section, all for one server."""
    reset_id()
    panels: list[dict] = []

    y = flow(panels, 0, [(24, 8, stat_grid(_STAT_ROW, cols=4))])

    y = subtab(
        panels,
        "Overview",
        y,
        [
            (
                24,
                7,
                lambda x, y, w, h: timeseries(
                    "CPU %",
                    x,
                    y,
                    w,
                    h,
                    [target(_cpu_lane_sql())],
                    unit="percent",
                    max_=105,
                    overrides=_BAND_OVERRIDES,
                ),
            ),
            (
                24,
                7,
                lambda x, y, w, h: timeseries(
                    "Wait ms/sec",
                    x,
                    y,
                    w,
                    h,
                    [target(_wait_lane_sql())],
                    axis_label="ms/sec",
                    overrides=_BAND_OVERRIDES,
                ),
            ),
            (
                24,
                7,
                lambda x, y, w, h: timeseries(
                    "Blocking & Deadlocking",
                    x,
                    y,
                    w,
                    h,
                    [target(_blocking_lane_sql())],
                    bars=True,
                    axis_label="events",
                    overrides=_BAND_OVERRIDES,
                ),
            ),
            (
                24,
                7,
                lambda x, y, w, h: timeseries(
                    "Buffer Pool MB",
                    x,
                    y,
                    w,
                    h,
                    [target(_MEMORY_LANE_SQL)],
                    unit="decmbytes",
                ),
            ),
            (
                24,
                7,
                lambda x, y, w, h: timeseries(
                    "I/O Latency",
                    x,
                    y,
                    w,
                    h,
                    [target(_file_io_lane_sql())],
                    unit="ms",
                    axis_label="ms",
                    overrides=_BAND_OVERRIDES,
                ),
            ),
        ],
    )

    y = subtab(
        panels,
        "Anomalies",
        y,
        [
            (
                24,
                8,
                lambda x, y, w, h: table(
                    "Anomalies",
                    x,
                    y,
                    w,
                    h,
                    _ANOMALIES_SQL,
                    overrides=[col_hidden("target_url"), _ANOMALY_DRILL],
                    sort_by=[{"displayName": "Start Time", "desc": True}],
                    description=(
                        "One row per contiguous baseline-anomaly event across the CPU, "
                        "Wait ms/sec, and I/O Latency lanes above (Blocking has no baseline "
                        "anomaly marker - see _blocking_lane_sql). Click Start Time to open "
                        "Query Performance's Active Queries at that event's window, padded "
                        "+/-5 minutes."
                    ),
                ),
            ),
        ],
    )

    subtab(
        panels,
        "History",
        y,
        [
            (
                24,
                7,
                lambda x, y, w, h: status_history(
                    "Performance Calendar",
                    x,
                    y,
                    w,
                    h,
                    _CALENDAR_SQL,
                    _DAILY_STATES,
                    description=(
                        "Composite daily health, one tile per day. A day with no "
                        "collection at all is absent rather than tiled. Click a day to "
                        "open Query Performance's Active Queries for that day's 24 hours."
                    ),
                    time_from=_HISTORY_WINDOW,
                    overrides=_CALENDAR_OVERRIDES,
                    links=_CALENDAR_DRILL,
                ),
            ),
            (
                24,
                14,
                lambda x, y, w, h: table(
                    "Daily Detail",
                    x,
                    y,
                    w,
                    h,
                    _DAILY_DETAIL_SQL,
                    time_from=_HISTORY_WINDOW,
                    overrides=[
                        status_colors("Health", HEALTH_STATUS_COLORS),
                        col_unit("Total Wait", "s"),
                        col_unit("Peak Block", "ms"),
                        col_thresholds("Deadlocks", ("text", None), ("red", 1)),
                        col_thresholds("Collection Errors", ("text", None), ("red", 1)),
                        col_thresholds(
                            "High-CPU Samples",
                            ("text", None),
                            ("yellow", _HIGH_CPU_WARNING),
                            ("red", _HIGH_CPU_CRITICAL),
                        ),
                        col_thresholds(
                            "Blocking Events",
                            ("text", None),
                            ("yellow", _BLOCKING_WARNING),
                            ("red", _BLOCKING_CRITICAL),
                        ),
                        col_thresholds("Severe Memory", ("text", None), ("red", 1)),
                    ],
                    sort_by=[{"displayName": "Day", "desc": True}],
                ),
            ),
        ],
    )

    # Default stays now-3h: this is the landing page after Fleet, and its "right now"
    # correlated lanes are the primary job. The History section panels carry their own
    # fixed _HISTORY_WINDOW override so they stay meaningful regardless of this default.
    return dashboard(uid("overview"), "Overview", panels, [server_var()])
