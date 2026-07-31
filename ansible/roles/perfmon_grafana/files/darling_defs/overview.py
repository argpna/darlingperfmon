"""Overview dashboard (Darling line) - per-server correlated timeline lanes.

Upstream ref: CorrelatedTimelineLanesControl.xaml(.cs), ViewerDataService.Cpu.cs,
.BlockingTrends.cs, .FileIo.cs, .OverviewLanes.cs, PgBaselineProvider.cs,
PerformanceMonitor.Analysis.Baselines.BaselineMath/BaselineBucket.

$server is single-select here (server_var(multi=False)), unlike the rest of this line:
upstream's Overview is one Viewer tab per server, and a baseline band's fillBelowTo
pairing needs an exact, statically-known partner series name - it can't address however
many series a multi-select variable resolves to at query time.

Not ported: the "Show Active Queries at This Time" right-click drill-down - Grafana
timeseries panels have no per-point click event.
"""

from ._shared import (
    collector,
    dashboard,
    reset_id,
    server_filter,
    server_var,
    series_style,
    subtab,
    target,
    timeseries,
    uid,
)

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


def overview():
    """Build the Overview dashboard - five correlated timeline lanes for one server."""
    reset_id()
    panels: list[dict] = []

    subtab(
        panels,
        "Overview",
        0,
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

    return dashboard(uid("overview"), "Overview", panels, [server_var(multi=False)])
