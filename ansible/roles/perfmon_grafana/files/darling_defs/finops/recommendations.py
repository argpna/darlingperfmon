"""FinOps Recommendations dashboard (Darling line).

Upstream ref: ViewerDataService.FinOps.Recommendations.cs.

Each of upstream's checks is one UNION ALL branch over a shared block of fact CTEs. Thresholds,
severities, confidences and wording are verbatim - a retuned threshold would silently disagree
with upstream's advice. Windows are fixed, since the thresholds were calibrated against them.
"""

from .._shared import (
    UTC_NOW,
    col_unit,
    collector,
    finops_dashboard,
    fixed,
    flow,
    n0,
    reset_id,
    server_filter,
    server_join,
    server_var,
    status_colors,
    table,
    uid,
)
from ._shared import (
    CONFIDENCE_COLORS,
    IDLE_DAYS,
    SEVERITY_COLORS,
    TREND_DAYS,
    budget_cte,
    fixed_window_tiers,
    format_duration,
    idle_db_ctes,
)

_PROPS = collector("server_properties")
_CPU = collector("cpu_utilization_stats")
_MEM = collector("memory_stats")
_SIZES = collector("database_size_stats")
_IOS = collector("index_object_stats")
_FILE_IO = collector("file_io_stats")
_JOBS = collector("running_jobs")
_DB_CONFIG = collector("database_config")
_QUERY_STATS = collector("query_stats")

_WINDOW = f"{UTC_NOW} - INTERVAL '{TREND_DAYS} days'"

# The four system databases - upstream's `database_id > 4` filter expressed by name.
_SYSTEM_DBS = "'master', 'model', 'msdb', 'tempdb'"

# Upstream's dev/test name patterns, case-insensitive.
_DEV_TEST_PATTERNS = ("dev", "test", "staging", "qa")

# BuildEditionAuditRecommendations' savings fraction and check 10's list-price differential.
_EDITION_SAVINGS_FRACTION = "0.40"
_LICENSE_USD_PER_CORE_YEAR = 5000

# BuildCompressionRecommendation's candidate floor.
_COMPRESSION_MIN_MB = 1024

# Samples upstream needs before it trusts a memory P95.
_MIN_MEM_SAMPLES = 16


def _top_list(arr: str, keep: int, sep: str = ", ") -> str:
    """Upstream's `string.Join(sep, x.Take(n))` + " and N more"."""
    return (
        f"array_to_string({arr}[1:{keep}], '{sep}')"
        f" || CASE WHEN COALESCE(array_length({arr}, 1), 0) > {keep}"
        f" THEN ' and ' || (array_length({arr}, 1) - {keep}) || ' more'"
        " ELSE '' END"
    )


def _pct0(ratio: str) -> str:
    """C# P0: a ratio rendered as a whole percentage."""
    return f"{fixed(f'({ratio}) * 100', 0)} || '%'"


_DEV_TEST_MATCH = " OR ".join(
    f"c.database_name ILIKE '%{pattern}%'" for pattern in _DEV_TEST_PATTERNS
)


def _idle_branch(relation: str, time_col: str) -> str:
    """The idle-database facts for one retention tier (check 6's inputs)."""
    return f"""
WITH {idle_db_ctes(relation, time_col)}
SELECT server_id,
       COUNT(*) AS idle_count,
       SUM(total_size_mb) AS idle_size_mb,
       array_agg(database_name ORDER BY total_size_mb DESC) AS idle_names
FROM idle_dbs
GROUP BY server_id
"""


_IDLE_FACTS = fixed_window_tiers(_idle_branch, IDLE_DAYS)

_FACTS = f"""
facts AS (
    /* RecommendationsEditionFactsSql. license_cpu_count is the raw logical count the audit
       prices, NOT the vCore-aware cpu_count the right-sizing checks use. */
    SELECT DISTINCT ON (server_id)
           server_id,
           COALESCE(edition, '') AS edition,
           COALESCE(split_part(product_version, '.', 1)::int, 0) AS major_version,
           COALESCE(cpu_count, 0) AS license_cpu_count,
           COALESCE(NULLIF(trim(ag_replica_role), ''), 'Standalone') AS ag_role,
           COALESCE(is_hadr_enabled, false) AS is_hadr_enabled,
           COALESCE(vcore_count, cpu_count, 0) AS cpu_count
    FROM {_PROPS}
    WHERE {server_filter()}
    ORDER BY server_id, collection_time DESC
),
util AS (
    /* GetUtilizationEfficiencyAsync's 24h CPU aggregate. */
    SELECT server_id,
           AVG(sqlserver_cpu_utilization::numeric) AS avg_cpu_pct,
           MAX(sqlserver_cpu_utilization) AS max_cpu_pct,
           PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY sqlserver_cpu_utilization)::numeric
               AS p95_cpu_pct
    FROM {_CPU}
    WHERE {server_filter()} AND collection_time >= {UTC_NOW} - INTERVAL '24 hours'
    GROUP BY server_id
),
phys AS (
    SELECT DISTINCT ON (server_id)
           server_id, COALESCE(total_physical_memory_mb, 0)::int AS physical_memory_mb
    FROM {_MEM}
    WHERE {server_filter()}
    ORDER BY server_id, collection_time DESC
),
mem7 AS (
    /* RecommendationsMemoryP95Sql. */
    SELECT server_id,
           PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY total_server_memory_mb)::int
               AS p95_mb,
           COUNT(*) AS sample_count
    FROM {_MEM}
    WHERE {server_filter()} AND collection_time >= {_WINDOW}
    GROUP BY server_id
),
cpu7 AS (
    /* RecommendationsCpuP95Sql plus RecommendationsReservedCapacitySql's mean/stddev. */
    SELECT server_id,
           PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY sqlserver_cpu_utilization)::numeric
               AS p95_cpu,
           AVG(sqlserver_cpu_utilization)::numeric AS avg_cpu,
           STDDEV(sqlserver_cpu_utilization)::numeric AS stddev_cpu,
           COUNT(*) AS sample_count
    FROM {_CPU}
    WHERE {server_filter()} AND collection_time >= {_WINDOW}
    GROUP BY server_id
),
db_config AS (
    /* Latest database-config snapshot. The timestamp column here is capture_time. */
    SELECT c.*
    FROM {_DB_CONFIG} AS c
    WHERE {server_filter('c.server_id')}
      AND (c.server_id, c.capture_time) IN (
          SELECT server_id, MAX(capture_time) FROM {_DB_CONFIG}
          WHERE {server_filter()} GROUP BY server_id)
),
tde AS (
    /* SelectTdeDatabaseNames - is_encrypted marks a TDE database. */
    SELECT server_id, array_agg(database_name ORDER BY lower(database_name)) AS names
    FROM db_config AS c
    WHERE COALESCE(c.is_encrypted, false)
      AND upper(COALESCE(c.state_desc, '')) = 'ONLINE'
      AND c.database_name NOT IN ({_SYSTEM_DBS})
    GROUP BY server_id
),
devtest AS (
    /* MatchDevTestDatabases. */
    SELECT server_id, array_agg(database_name) AS names
    FROM db_config AS c
    WHERE c.database_name NOT IN ({_SYSTEM_DBS})
      AND ({_DEV_TEST_MATCH})
    GROUP BY server_id
),
idle AS ({_IDLE_FACTS}),
size_totals AS (
    SELECT server_id, SUM(total_size_mb) AS total_mb
    FROM {_SIZES}
    WHERE {server_filter()}
      AND (server_id, collection_time) IN (
          SELECT server_id, MAX(collection_time) FROM {_SIZES}
          WHERE {server_filter()} GROUP BY server_id)
    GROUP BY server_id
),
compress AS (
    /* BuildCompressionRecommendation over GetIndexCleanupInputsAsync' snapshot.
       data_compression_desc = 'NONE' also excludes columnstore. */
    SELECT server_id,
           COUNT(*) AS candidate_count,
           SUM(reserved_mb) / 1024.0 AS total_gb,
           array_agg(schema_name || '.' || table_name || ' ('
                     || {fixed('reserved_mb / 1024.0', 1)} || 'GB)'
                     ORDER BY reserved_mb DESC) AS items
    FROM (
        SELECT DISTINCT ON (server_id, database_id, object_id, index_id)
               server_id, schema_name, table_name, reserved_mb, data_compression_desc
        FROM {_IOS}
        WHERE {server_filter()}
        ORDER BY server_id, database_id, object_id, index_id, collection_time DESC
    ) latest_ix
    WHERE upper(COALESCE(data_compression_desc, '')) = 'NONE'
      AND COALESCE(reserved_mb, 0) >= {_COMPRESSION_MIN_MB}
    GROUP BY server_id
),
jobs AS (
    /* RecommendationsMaintenanceWindowSql. */
    SELECT server_id,
           job_name,
           AVG(current_duration_seconds)::bigint AS avg_duration_seconds,
           MAX(current_duration_seconds)::bigint AS max_duration_seconds,
           AVG(avg_duration_seconds)::bigint AS avg_historical,
           SUM(CASE WHEN is_running_long THEN 1 ELSE 0 END) AS times_ran_long
    FROM {_JOBS}
    WHERE {server_filter()}
      AND collection_time >= {_WINDOW}
      AND avg_duration_seconds > 0
    GROUP BY server_id, job_name
    HAVING SUM(CASE WHEN is_running_long THEN 1 ELSE 0 END) >= 3
),
io7 AS (
    /* RecommendationsStorageTierSql, with its per-database latency test applied. */
    SELECT server_id, database_name,
           SUM(delta_stall_read_ms)::numeric / NULLIF(SUM(delta_reads), 0) AS avg_read_ms,
           SUM(delta_stall_write_ms)::numeric / NULLIF(SUM(delta_writes), 0) AS avg_write_ms
    FROM {_FILE_IO}
    WHERE {server_filter()} AND collection_time >= {_WINDOW} AND delta_reads > 0
    GROUP BY server_id, database_name
    HAVING SUM(delta_reads) > 1000
),
low_latency AS (
    SELECT server_id,
           COUNT(*) AS db_count,
           array_agg(database_name || ' (read ' || {fixed('avg_read_ms', 1)}
                     || 'ms, write ' || {fixed('COALESCE(avg_write_ms, 0)', 1)} || 'ms)'
                     ORDER BY database_name) AS items
    FROM io7
    WHERE avg_read_ms < 5 AND COALESCE(avg_write_ms, 0) < 3
    GROUP BY server_id
),
{budget_cte()},
server_facts AS (
    SELECT f.*,
           COALESCE(b.monthly_cost, 0) AS monthly_cost,
           u.avg_cpu_pct, u.max_cpu_pct, u.p95_cpu_pct,
           p.physical_memory_mb,
           m.p95_mb AS mem_p95_mb, COALESCE(m.sample_count, 0) AS mem_sample_count,
           /* VM right-sizing prefers the 7-day P95, falling back to the 24h one. */
           COALESCE(c7.p95_cpu, u.p95_cpu_pct) AS p95_cpu_7d,
           c7.avg_cpu AS avg_cpu_7d, c7.stddev_cpu, COALESCE(c7.sample_count, 0) AS cpu_samples_7d,
           t.names AS tde_names,
           d.names AS devtest_names,
           i.idle_count, i.idle_size_mb, i.idle_names,
           st.total_mb AS total_size_mb,
           cp.candidate_count AS compress_count, cp.total_gb AS compress_gb,
           cp.items AS compress_items,
           ll.db_count AS low_latency_count, ll.items AS low_latency_items
    FROM facts f
    LEFT JOIN budget b ON b.server_id = f.server_id
    LEFT JOIN util u ON u.server_id = f.server_id
    LEFT JOIN phys p ON p.server_id = f.server_id
    LEFT JOIN mem7 m ON m.server_id = f.server_id
    LEFT JOIN cpu7 c7 ON c7.server_id = f.server_id
    LEFT JOIN tde t ON t.server_id = f.server_id
    LEFT JOIN devtest d ON d.server_id = f.server_id
    LEFT JOIN idle i ON i.server_id = f.server_id
    LEFT JOIN size_totals st ON st.server_id = f.server_id
    LEFT JOIN compress cp ON cp.server_id = f.server_id
    LEFT JOIN low_latency ll ON ll.server_id = f.server_id
)"""

# BuildEditionAuditRecommendations' AG state. Basic AGs are Standard-only, so an Enterprise
# primary with Always On on effectively always hosts an advanced AG - upstream's own stand-in
# for the non-basic availability_groups count it cannot collect.
_IS_ENTERPRISE = "s.edition ILIKE '%Enterprise%'"
_IS_SECONDARY = "lower(s.ag_role) = 'secondary'"
_HAS_ADVANCED_AG = (
    f"(NOT {_IS_SECONDARY} AND lower(s.ag_role) = 'primary' AND s.is_hadr_enabled)"
)

_AG_CAVEAT = f"""CASE WHEN {_HAS_ADVANCED_AG} THEN
        ' Note: this instance is the primary replica of an Always On Availability Group.'
        || ' Standard Edition supports only Basic Availability Groups, which are limited to'
        || ' two replicas, a single database per group, and provide no readable secondary or'
        || ' backups on the secondary (see https://learn.microsoft.com/en-us/sql/'
        || 'database-engine/availability-groups/windows/basic-availability-groups-always-on-'
        || 'availability-groups#limitations). Factor this into any downgrade decision.'
    ELSE '' END"""

_TDE_COUNT = "COALESCE(array_length(s.tde_names, 1), 0)"

_BRANCHES = [
    # Check 1a: Enterprise on an AG secondary.
    f"""
SELECT s.server_id, 'Licensing' AS category, 'Low' AS severity, 'High' AS confidence,
       'Enterprise Edition - Availability Group secondary replica' AS finding,
       'This instance is currently a secondary replica in an Availability Group. Every '
       || 'replica in an AG must run the same SQL Server edition, so edition and licensing '
       || 'decisions apply to the whole group and should be evaluated on the primary '
       || 'replica. A secondary used only for failover may also be covered by Software '
       || 'Assurance rather than separately licensed.' AS detail,
       NULL::numeric AS est_savings
FROM server_facts s
WHERE {_IS_ENTERPRISE} AND {_IS_SECONDARY}
""",
    # Check 1b: 2019+ - most Enterprise-only features moved to Standard.
    f"""
SELECT s.server_id, 'Licensing', 'High',
       CASE WHEN {_HAS_ADVANCED_AG} THEN 'Low' ELSE 'Medium' END,
       'Enterprise Edition may not be required',
       'Starting with SQL Server 2019, most previously Enterprise-only features (including '
       || 'TDE, compression, partitioning, and columnstore) are available in Standard '
       || 'Edition. Review whether remaining Enterprise-only features (such as Always On '
       || 'availability groups with multiple secondaries) are in use before considering a '
       || 'downgrade to Standard Edition.' || {_AG_CAVEAT},
       CASE WHEN s.monthly_cost > 0
            THEN s.monthly_cost * {_EDITION_SAVINGS_FRACTION} END
FROM server_facts s
WHERE {_IS_ENTERPRISE} AND NOT {_IS_SECONDARY} AND s.major_version >= 15
""",
    # Check 1c: pre-2019 with no TDE, so nothing blocks a downgrade.
    f"""
SELECT s.server_id, 'Licensing', 'High',
       CASE WHEN {_HAS_ADVANCED_AG} THEN 'Medium' ELSE 'High' END,
       CASE WHEN {_HAS_ADVANCED_AG}
            THEN 'Enterprise Edition - review Availability Group requirements before '
                 || 'downgrading'
            ELSE 'Enterprise Edition with no Enterprise-only features detected' END,
       'No databases use Transparent Data Encryption (TDE), the only feature still '
       || 'restricted to Enterprise Edition since SQL Server 2016 SP1. Review whether '
       || 'Standard Edition would meet workload requirements for potential license savings.'
       || {_AG_CAVEAT},
       CASE WHEN s.monthly_cost > 0
            THEN s.monthly_cost * {_EDITION_SAVINGS_FRACTION} END
FROM server_facts s
WHERE {_IS_ENTERPRISE} AND NOT {_IS_SECONDARY} AND s.major_version < 15
  AND {_TDE_COUNT} = 0
""",
    # Check 1d: pre-2019 with TDE in use.
    f"""
SELECT s.server_id, 'Licensing', 'Low', 'High',
       'TDE in use - Enterprise Edition downgrade blocker',
       'The following databases use Transparent Data Encryption: '
       || {_top_list('s.tde_names', 20)}
       || '. TDE must be removed before downgrading to Standard Edition.',
       NULL::numeric
FROM server_facts s
WHERE {_IS_ENTERPRISE} AND NOT {_IS_SECONDARY} AND s.major_version < 15
  AND {_TDE_COUNT} > 0
""",
    # Check 10: list-price differential, emitted only alongside 1d.
    f"""
SELECT s.server_id, 'Licensing', 'Low', 'Low',
       'Enterprise to Standard would save ~$'
       || {n0(f's.license_cpu_count * {_LICENSE_USD_PER_CORE_YEAR} / 12.0')}
       || '/mo at list pricing (' || s.license_cpu_count || ' cores)',
       'Based on list pricing differential of ~${_LICENSE_USD_PER_CORE_YEAR}/core/year '
       || 'between Enterprise and Standard. Actual savings depend on your licensing '
       || 'agreement. See Enterprise feature audit for downgrade blockers.',
       s.license_cpu_count * {_LICENSE_USD_PER_CORE_YEAR} / 12.0
FROM server_facts s
WHERE {_IS_ENTERPRISE} AND NOT {_IS_SECONDARY} AND s.major_version < 15
  AND {_TDE_COUNT} > 0 AND s.license_cpu_count > 0
""",
    # Check 2: CPU right-sizing.
    f"""
SELECT t.server_id, 'Compute',
       CASE WHEN t.p95_cpu_pct < 15 THEN 'High' ELSE 'Medium' END, 'Medium',
       'CPU over-provisioned (' || t.cpu_count || ' cores, P95 = '
       || {fixed('t.p95_cpu_pct', 1)} || '%)',
       'P95 CPU utilization is ' || {fixed('t.p95_cpu_pct', 1)} || '% (avg '
       || {fixed('t.avg_cpu_pct', 1)} || '%, max ' || t.max_cpu_pct || '%) across '
       || t.cpu_count || ' cores. Consider reducing to ~' || t.target_cores || ' cores.',
       CASE WHEN t.monthly_cost > 0
            THEN t.monthly_cost * (1 - t.target_cores::numeric / t.cpu_count) * 0.60 END
FROM (
    SELECT s.*,
           GREATEST(4, trunc(s.cpu_count * (s.p95_cpu_pct / 70.0))::int) AS target_cores
    FROM server_facts s
    WHERE s.p95_cpu_pct < 30 AND s.cpu_count > 4
) t
""",
    # Check 3: memory right-sizing.
    f"""
SELECT t.server_id, 'Memory',
       CASE WHEN t.mem_ratio < 0.30 THEN 'High' ELSE 'Medium' END, 'Medium',
       'Memory over-provisioned (P95 SQL memory uses ' || {_pct0('t.mem_ratio')} || ' of '
       || (t.physical_memory_mb / 1024) || 'GB RAM)',
       'P95 SQL Server memory over {TREND_DAYS} days is ' || {n0('t.mem_p95_mb')}
       || ' MB out of ' || {n0('t.physical_memory_mb')} || ' MB physical RAM ('
       || {_pct0('t.mem_ratio')} || ' utilization). Consider reducing to ~'
       || (t.target_mb / 1024) || 'GB.',
       CASE WHEN t.monthly_cost > 0
            THEN t.monthly_cost
                 * (1 - t.target_mb::numeric / t.physical_memory_mb) * 0.30 END
FROM (
    SELECT s.*,
           s.mem_p95_mb::numeric / s.physical_memory_mb AS mem_ratio,
           GREATEST(8192, s.mem_p95_mb * 2) AS target_mb
    FROM server_facts s
    WHERE s.physical_memory_mb > 8192
      AND s.mem_sample_count >= {_MIN_MEM_SAMPLES}
) t
WHERE t.mem_ratio < 0.50
""",
    # Check 5: compression candidates.
    f"""
SELECT s.server_id, 'Storage',
       CASE WHEN s.compress_gb > 50 THEN 'High'
            WHEN s.compress_gb > 10 THEN 'Medium' ELSE 'Low' END, 'High',
       s.compress_count || ' uncompressed object(s) >= 1GB ('
       || {fixed('s.compress_gb', 1)} || 'GB total)',
       'Large uncompressed tables/indexes: ' || {_top_list('s.compress_items', 5, '; ')}
       || '. Consider PAGE or ROW compression to reduce storage and improve I/O.',
       NULL::numeric
FROM server_facts s
WHERE COALESCE(s.compress_count, 0) > 0
""",
    # Check 6: dormant databases.
    f"""
SELECT s.server_id, 'Databases',
       CASE WHEN s.idle_count >= 3 THEN 'High' ELSE 'Medium' END, 'High',
       s.idle_count || ' idle database(s) consuming '
       || {fixed('s.idle_size_mb / 1024.0', 1)} || 'GB',
       'No query activity in {TREND_DAYS} days: ' || {_top_list('s.idle_names', 5)}
       || '. Consider archiving or removing these databases.',
       CASE WHEN s.monthly_cost > 0 AND COALESCE(s.total_size_mb, 0) > 0
            THEN NULLIF(s.idle_size_mb / s.total_size_mb * s.monthly_cost, 0) END
FROM server_facts s
WHERE COALESCE(s.idle_count, 0) > 0
""",
    # Check 7: dev/test databases on a production instance.
    f"""
SELECT s.server_id, 'Environment', 'Medium', 'Low',
       COALESCE(array_length(s.devtest_names, 1), 0)
       || ' possible dev/test database(s) on production server',
       'Databases matching dev/test patterns: ' || {_top_list('s.devtest_names', 10)}
       || '. If these are non-production workloads, consider moving to a lower-cost tier '
       || 'or separate server.',
       NULL::numeric
FROM server_facts s
WHERE COALESCE(array_length(s.devtest_names, 1), 0) > 0
""",
    # Check 11: maintenance window, one row per job that ran long.
    f"""
SELECT j.server_id, 'Maintenance',
       CASE WHEN j.times_ran_long >= 5 THEN 'Medium' ELSE 'Low' END, 'High',
       j.job_name || ' ran long ' || j.times_ran_long || ' times in {TREND_DAYS} days',
       'Average duration: ' || {format_duration('j.avg_duration_seconds')} || ', max: '
       || {format_duration('j.max_duration_seconds')} || ', historical average: '
       || {format_duration('j.avg_historical')}
       || '. Review whether this job''s schedule or operations need tuning.',
       NULL::numeric
FROM jobs j
""",
    # Check 12a: VM right-sizing, CPU.
    f"""
SELECT t.server_id, 'Hardware', 'Medium', 'Medium',
       'CPU: reduce from ' || t.cpu_count || ' to ' || t.target_cores || ' cores (P95 CPU '
       || {fixed('t.p95_cpu_7d', 1)} || '%)',
       'Over the last {TREND_DAYS} days, P95 CPU utilization was '
       || {fixed('t.p95_cpu_7d', 1)} || '%. Current allocation of ' || t.cpu_count
       || ' cores can safely be reduced to ' || t.target_cores || ' cores.',
       CASE WHEN t.monthly_cost > 0
            THEN t.monthly_cost * (1 - t.target_cores::numeric / t.cpu_count) * 0.50 END
FROM (
    SELECT s.*,
           CASE WHEN s.p95_cpu_7d < 15 THEN GREATEST(2, s.cpu_count / 4)
                WHEN s.p95_cpu_7d < 30 THEN GREATEST(2, s.cpu_count / 2)
                ELSE 0 END AS target_cores
    FROM server_facts s
    WHERE s.cpu_count >= 4 AND s.p95_cpu_7d IS NOT NULL
) t
WHERE t.target_cores > 0 AND t.target_cores < t.cpu_count
""",
    # Check 12b: VM right-sizing, memory.
    f"""
SELECT t.server_id, 'Hardware', 'Medium', 'Medium',
       'Memory: reduce from ' || (t.physical_memory_mb / 1024) || 'GB to '
       || (t.target_mb / 1024) || 'GB (P95 SQL memory uses ' || {_pct0('t.mem_ratio')} || ')',
       'P95 SQL Server memory over {TREND_DAYS} days is ' || {n0('t.mem_p95_mb')} || ' MB of '
       || {n0('t.physical_memory_mb')} || ' MB physical RAM (' || {_pct0('t.mem_ratio')}
       || '). Reducing to ' || (t.target_mb / 1024) || 'GB would still leave headroom.',
       CASE WHEN t.monthly_cost > 0
            THEN t.monthly_cost
                 * (1 - t.target_mb::numeric / t.physical_memory_mb) * 0.30 END
FROM (
    SELECT s.*,
           s.mem_p95_mb::numeric / s.physical_memory_mb AS mem_ratio,
           CASE WHEN s.mem_p95_mb::numeric / s.physical_memory_mb < 0.25
                THEN GREATEST(4096, s.physical_memory_mb / 4)
                WHEN s.mem_p95_mb::numeric / s.physical_memory_mb < 0.40
                THEN GREATEST(4096, s.physical_memory_mb / 2)
                ELSE 0 END AS target_mb
    FROM server_facts s
    WHERE s.physical_memory_mb >= 4096
      AND s.mem_sample_count >= {_MIN_MEM_SAMPLES}
) t
WHERE t.target_mb > 0 AND t.target_mb < t.physical_memory_mb
""",
    # Check 13: storage tier - consistently low-latency databases.
    f"""
SELECT s.server_id, 'Storage', 'Low', 'Medium',
       s.low_latency_count
       || ' database(s) with low IO latency - standard storage may suffice',
       'These databases have avg read latency under 5ms and write under 3ms over '
       || '{TREND_DAYS} days: ' || {_top_list('s.low_latency_items', 10, '; ')}
       || '. Premium/high-performance storage may not be needed.',
       NULL::numeric
FROM server_facts s
WHERE COALESCE(s.low_latency_count, 0) > 0
""",
    # Check 14: reserved capacity - low coefficient of variation.
    f"""
SELECT t.server_id, 'Cloud', 'Low',
       CASE WHEN t.cv < 0.15 THEN 'High' ELSE 'Medium' END,
       'Stable CPU utilization (avg ' || {fixed('t.avg_cpu_7d', 1)} || '%, CV '
       || {fixed('t.cv', 2)} || ') - reserved capacity candidate',
       'CPU utilization is consistently ' || {fixed('t.avg_cpu_7d', 1)}
       || '% with low variance (+/-' || {fixed('t.stddev_cpu', 1)}
       || '%). Reserved pricing typically saves 30-40% over pay-as-you-go for predictable '
       || 'workloads.',
       NULL::numeric
FROM (
    SELECT s.*, s.stddev_cpu / s.avg_cpu_7d AS cv
    FROM server_facts s
    WHERE s.cpu_samples_7d >= 24 AND s.avg_cpu_7d > 20 AND s.stddev_cpu > 0
) t
WHERE t.cv < 0.3
""",
]

_FINDINGS = "\nUNION ALL\n".join(b.strip() for b in _BRANCHES)

# RecommendationRow.SeveritySort.
_SEVERITY_SORT = """CASE r.severity
        WHEN 'High' THEN 1 WHEN 'Medium' THEN 2 WHEN 'Low' THEN 3 ELSE 4
    END"""

_SQL = f"""
WITH {_FACTS},
findings AS (
{_FINDINGS}
)
SELECT
    srv.name AS "Server",
    r.category AS "Category",
    r.severity AS "Severity",
    r.confidence AS "Confidence",
    r.finding AS "Finding",
    r.detail AS "Detail",
    round(r.est_savings, 0) AS "Est. Savings ($/mo)"
FROM findings r
{server_join('r.server_id')}
ORDER BY srv.name, {_SEVERITY_SORT}, r.category
"""


def recommendations():
    """Build the FinOps Recommendations dashboard."""
    reset_id()
    panels: list[dict] = []

    flow(
        panels,
        0,
        [
            (
                24,
                20,
                lambda x, y, w, h: table(
                    "Cost-Saving Recommendations",
                    x,
                    y,
                    w,
                    h,
                    _SQL,
                    overrides=[
                        status_colors("Severity", SEVERITY_COLORS),
                        status_colors("Confidence", CONFIDENCE_COLORS),
                        col_unit("Est. Savings ($/mo)", "currencyUSD"),
                    ],
                    description=(
                        "From collected data - no live target. Thresholds and wording are "
                        "upstream's. Est. Savings is blank until a budget is configured. "
                        "Upstream's sp_IndexCleanup-existence check is dropped, superseded "
                        "by the Index Analysis dashboard."
                    ),
                ),
            )
        ],
    )

    return finops_dashboard(
        uid("finops-recommendations"),
        "Recommendations",
        panels,
        [server_var()],
        refresh="15m",
    )
