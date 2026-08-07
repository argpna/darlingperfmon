"""Shared helpers for the Darling (PostgreSQL) dashboards.

One datasource for the whole store; instance selection is the $server variable.
Read the column and retention notes below before writing panel SQL.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
# pylint: disable=wrong-import-position
from panel_kit import (  # noqa: E402
    PanelKit,
    col_datalink,
    col_datalinks,
    col_gauge_bar,
    col_hidden,
    col_thresholds,
    col_unit,
    series_style,
    status_colors,
    text_var,
)

# Dedicated dashboard-JSON directory and uid prefix.
_DASHBOARDS_ROOT = (
    pathlib.Path(__file__).resolve().parent.parent / "grafana" / "dashboards"
)
OUT = _DASHBOARDS_ROOT / "darling"

UID_PREFIX = "darling"


def uid(name: str) -> str:
    """Namespace a dashboard uid with the darling prefix."""
    return f"{UID_PREFIX}-{name}"


DS_UID = "darling"
DS = {"type": "grafana-postgresql-datasource", "uid": DS_UID}

HEALTH_STATUS_COLORS = {
    "Healthy": "green",
    "Warning": "yellow",
    "Critical": "red",
    "Unknown": "text",
}

DURATION_STATUS_COLORS = {
    "OK": "green",
    "Slow": "yellow",
    "Stalled": "red",
}

# collection_time is UTC in a naive `timestamp` column, so $__timeFilter() is correct as-is
# and the MSSQL line's tz_* helpers have no counterpart here. cpu_utilization_stats'
# sample_time is the exception - it is server-local and needs the de-skew cpu.py carries.

# Retention tiers - a panel outrunning its table's horizon returns a short series, not an
# error. Every collector table is purged at its own horizon (30d for most), but only
# query_stats/procedure_stats/query_store_stats drop raw EARLIER than their rollups reach
# (4d raw, 21d *_hourly, indefinite *_daily), so tiered() is for those three alone.
#
# *_baseline aggregates are NOT a retention tier - they are a separate shape built for
# anomaly detection (cpu_utilization_baseline carries sum/sumsq/count for stddev). Read
# them directly, never through rollup().
#
# Upstream's router: Darling.Service/Compose/ComposeSourceRouter.cs.

# Delta columns are `delta_` PREFIXED and the rename from the MSSQL schema is not
# mechanical (waiting_tasks_count_delta -> delta_waiting_tasks). Check the column list
# before porting a query. Collector tables carry server_name alongside server_id.
# The per-table cumulative-vs-gauge audit has not been redone for Darling yet.


# Collector tables with no collect.v_* view. Re-derive with:
#   SELECT t.table_name FROM information_schema.tables t
#   WHERE t.table_schema='collect' AND t.table_type='BASE TABLE'
#     AND NOT EXISTS (SELECT 1 FROM information_schema.views v
#                     WHERE v.table_schema='collect' AND v.table_name='v_'||t.table_name);
_NO_VIEW = frozenset(
    {
        "ag_database_replica_states",
        "ag_replica_states",
        "agent_status",
        "analysis_findings",
        "analysis_state",
        "darling_schema_version",
        "default_trace_events",
        "job_history",
        "long_query_completions",
        "module_map",
        "procedure_stats",
        "query_plan_dim",
        "query_text_dim",
        "server_properties",
        "servers",
        "waiting_tasks",
    }
)

_ROLLUP_TIERS = ("hourly", "daily")

# Route thresholds, aliased from upstream RetentionTierRouter (RawMaxAge/HourlyMaxAge).
# Each sits a day inside its tier's retention so a lagging chunk drop can never leave the
# chosen tier missing the window's oldest point.
RAW_MAX_AGE_DAYS = 3
HOURLY_MAX_AGE_DAYS = 20

# Which raw tables have rollups, and the dimensions those rollups GROUP BY. A panel may
# route to a CAGG only when every dimension it groups or filters on is covered here -
# otherwise the rollup cannot reproduce the result. server_id/server_name lead every CAGG.
_CAGG_DIMENSIONS = {
    "query_stats": {"database_name", "query_hash", "sql_handle"},
    "query_stats_db": {"database_name"},
    "procedure_stats": {"database_name", "schema_name", "object_name"},
    "query_store_stats": {"database_name", "module_name", "query_hash"},
}

# collection_time on a raw table; every CAGG's time dimension is the bucket it produced.
CAGG_TIME_COL = "bucket"

# Where a CAGG family's raw table is named differently: query_stats_db_* aggregates
# query_stats, and there is no collect.query_stats_db to measure a raw floor on.
_CAGG_RAW_BASE = {"query_stats_db": "query_stats"}

# Store timestamps are naive UTC, so "now" is taken in UTC too.
UTC_NOW = "(now() AT TIME ZONE 'UTC')"


def collector(base: str) -> str:
    """Resolve the relation for a raw collector table, preferring its v_* view."""
    return f"collect.{base}" if base in _NO_VIEW else f"collect.v_{base}"


def rollup(base: str, tier: str) -> str:
    """Resolve a continuous aggregate, for ranges beyond the raw table's retention."""
    if tier not in _ROLLUP_TIERS:
        raise ValueError(
            f"unknown rollup tier {tier!r}, expected one of {_ROLLUP_TIERS}"
        )
    if base not in _CAGG_DIMENSIONS:
        raise ValueError(f"{base!r} has no continuous aggregate")
    return f"collect.{base}_{tier}"


def cagg_covers(base: str, dimensions) -> bool:
    """True when a CAGG can reproduce a panel grouped or filtered on `dimensions`."""
    known = _CAGG_DIMENSIONS.get(base)
    return known is not None and set(dimensions) <= known


# Tiers finest-first, with the window age at which each takes over.
_TIERS = (("raw", 0), ("hourly", RAW_MAX_AGE_DAYS), ("daily", HOURLY_MAX_AGE_DAYS))


def _tier_bands(present: set[str]) -> dict[str, tuple[int, int | None]]:
    """Assign each age band to a present tier, returning per-tier (lo, hi) day bounds.

    A band whose own tier is absent falls to the next coarser tier present, since that one
    still retains the window; only when no coarser tier exists does it fall back to a finer
    one. Bands assigned to a tier are contiguous, so each becomes one range.
    """
    order = [name for name, _ in _TIERS]
    bands: dict[str, tuple[int, int | None]] = {}
    for index, (_, start) in enumerate(_TIERS):
        coarser = [t for t in order[index:] if t in present]
        finer = [t for t in reversed(order[:index]) if t in present]
        owner = (coarser or finer)[0]
        end = _TIERS[index + 1][1] if index + 1 < len(_TIERS) else None
        if owner in bands:
            bands[owner] = (bands[owner][0], end)
        else:
            bands[owner] = (start, end)
    return bands


def tier_guard(tier: str, present: set[str] | None = None) -> str:
    """Predicate selecting exactly one tier, by the age of the window's oldest point.

    Routing is by age, not by display grain: a purely historical window must reach the tier
    that still retains it. Grafana substitutes a literal for $__timeFrom(), so the predicate
    constant-folds to a One-Time Filter and an unselected branch is never executed.
    """
    present = present or {name for name, _ in _TIERS}
    if tier not in present:
        raise ValueError(f"unknown tier {tier!r}, expected raw, hourly or daily")
    lo, hi = _tier_bands(present)[tier]
    clauses = []
    if lo:
        clauses.append(f"$__timeFrom() < {UTC_NOW} - INTERVAL '{lo} days'")
    if hi is not None:
        clauses.append(f"$__timeFrom() >= {UTC_NOW} - INTERVAL '{hi} days'")
    return " AND ".join(clauses) if clauses else "true"


def _floor(base: str, tier: str) -> str:
    """How far back a tier has actually materialized, as an uncorrelated scalar subquery.

    Keep it uncorrelated: Postgres lifts it into an InitPlan, so a guard built from these
    still collapses to a One-Time Filter and an unrouted branch is never executed. Tie it to
    an outer-query column and that folding stops.
    """
    if tier == "raw":
        return (
            "(SELECT min(collection_time) FROM "
            f"{collector(_CAGG_RAW_BASE.get(base, base))})"
        )
    return f"(SELECT min({CAGG_TIME_COL}) FROM {rollup(base, tier)})"


def _resolved_tier(base: str, present: set[str]) -> str:
    """Expression yielding the tier that can actually answer the window.

    Age picks a tier; measured coverage can then demote it. A rollup built WITH NO DATA over
    pre-existing history serves only what it materialized, so age alone routes windows onto a
    rollup that answers them EMPTY while raw still holds every row. The demotion is
    comparative, never absolute: a tier is abandoned only when a lower one is measured to
    reach further back, which keeps a healthy store (raw ~4 days against the rollups' weeks)
    from dropping to raw and returning less.

    Upstream ref: RetentionTierRouter.Resolve, TierCoverage (#1759). The availability half of
    upstream's gate (#1664) is not portable here - a store without the rollups cannot even
    parse SQL naming them, so that one needs a build-time decision, not a runtime predicate.
    """
    covers = {t: f"{_floor(base, t)} <= $__timeFrom()" for t in present}
    infinity = "'infinity'::timestamp"

    def reaches(candidate: str, incumbent: str) -> str:
        return f"{_floor(base, candidate)} < COALESCE({_floor(base, incumbent)}, {infinity})"

    def demote(tier: str) -> str:
        """The coverage ladder below `tier`, mirroring the C# fallthrough order."""
        if tier == "raw":
            return "'raw'"
        rungs = [f"WHEN {covers[tier]} THEN '{tier}'"]
        if tier == "daily" and "hourly" in present:
            deeper = reaches("hourly", "daily")
            rungs.append(f"WHEN {deeper} AND {covers['hourly']} THEN 'hourly'")
            if "raw" in present:
                rungs.append(f"WHEN {deeper} AND {reaches('raw', 'hourly')} THEN 'raw'")
            rungs.append(f"WHEN {deeper} THEN 'hourly'")
        if "raw" in present:
            rungs.append(f"WHEN {reaches('raw', tier)} THEN 'raw'")
        return "CASE " + " ".join(rungs) + f" ELSE '{tier}' END"

    ordered = [name for name, _ in _TIERS if name in present]
    arms = " ".join(
        f"WHEN {tier_guard(tier, present)} THEN {demote(tier)}" for tier in ordered
    )
    return f"CASE {arms} END"


def tiered(branches: dict[str, str], base: str | None = None) -> str:
    """Combine per-tier SELECTs into one query, guarded so exactly one tier runs.

    branches maps tier ("raw"/"hourly"/"daily") to a complete SELECT. Each is wrapped in its
    guard rather than the caller appending one, so a branch cannot escape routing and
    duplicate the result. An omitted tier's age range falls to the next coarser branch.

    base names the raw collector table the branches read. Pass it whenever the rollups exist,
    so routing is gated on measured coverage as well as age - see _resolved_tier(). Age-only
    routing is what returns an empty panel on a store whose rollups have not yet materialized
    the window.

    Branch projections must agree on column names and order; the CAGGs reshape their metrics
    (delta columns become *_sum/*_min/*_max over CAGG_TIME_COL), so each branch spells out
    its own mapping - see rollup() and cagg_covers() for what a tier can serve.
    """
    unknown = set(branches) - {name for name, _ in _TIERS}
    if unknown:
        raise ValueError(f"unknown tier(s) {sorted(unknown)}")
    if not branches:
        raise ValueError("tiered() needs at least one branch")
    present = set(branches)
    resolved = _resolved_tier(base, present) if base else None
    parts = [
        f"SELECT * FROM (\n{sql.strip()}\n) AS {tier}_tier"
        f" WHERE {f'({resolved}) = {tier!r}' if resolved else tier_guard(tier, present)}"
        for tier, sql in sorted(branches.items())
    ]
    return "\nUNION ALL\n".join(parts)


def server_filter(col: str = "server_id") -> str:
    """Filter to the instance(s) selected in $server.

    :csv, not :sqlstring - server_id is an integer, and quoting would fail the comparison.
    """
    return f"{col} IN (${{server:csv}})"


SERVER_REGISTRY = "config.config_monitored_servers"


def server_join(col: str, alias: str = "srv") -> str:
    """Join the fleet registry for the server name to label a series or column with.

    collect.* denormalizes server_name as the HOST; the registry's name is what $server
    shows, so labels come from there and the two always agree.
    """
    return f"JOIN {SERVER_REGISTRY} AS {alias} ON {alias}.server_id = {col}"


def multi_filter(col: str, var: str) -> str:
    """Filter a text column by a multi-select variable."""
    arr = f"ARRAY[${{{var}:sqlstring}}]::text[]"
    return f"(cardinality({arr}) = 0 OR {col} = ANY({arr}))"


def n0(expr: str) -> str:
    """C# N0: thousands-separated integer."""
    return f"to_char({expr}, 'FM999,999,999,990')"


def fixed(expr: str, places: int) -> str:
    """C# F1/F2: fixed decimal places, no thousands separator."""
    mask = "FM999999990" + ("." + "0" * places if places else "")
    return f"to_char(round(({expr})::numeric, {places}), '{mask}')"


def duration_ms(expr: str) -> str:
    """Compact ms duration, matching DailyHealthBandCalculator.FormatDurationMs."""
    return f"""CASE
        WHEN {expr} < 1000 THEN {n0(expr)} || ' ms'
        WHEN {expr} < 60000 THEN {fixed(f'({expr}) / 1000.0', 1)} || ' s'
        ELSE {fixed(f'({expr}) / 60000.0', 1)} || ' min'
    END"""


def time_bucket(interval: str, col: str = "collection_time") -> str:
    """TimescaleDB time_bucket() - takes arbitrary intervals, unlike date_trunc."""
    return f"time_bucket(INTERVAL '{interval}', {col})"


_SHOWPLAN_NS = "http://schemas.microsoft.com/sqlserver/2004/07/showplan"


def plan_parameters_sql(plan_source_sql: str) -> str:
    """Compile-time parameter values parsed out of a stored plan XML."""
    return f"""
WITH plan AS ( {plan_source_sql} ),
parsed AS (SELECT plan_xml::xml AS doc FROM plan WHERE plan_xml IS NOT NULL),
ns AS (SELECT ARRAY[ARRAY['sp', '{_SHOWPLAN_NS}']] AS n),
params AS (
    SELECT unnest(xpath('//sp:ParameterList/sp:ColumnReference', doc, ns.n)) AS node
    FROM parsed, ns
)
SELECT
    (xpath('/*/@Column', node))[1]::text AS "Parameter",
    (xpath('/*/@ParameterCompiledValue', node))[1]::text AS "Compiled Value",
    (xpath('/*/@ParameterRuntimeValue', node))[1]::text AS "Runtime Value"
FROM params
"""


def target(sql: str, fmt: str = "time_series", ref: str = "A") -> dict:
    """Build a Grafana query target. No SQL rewriting: no tz shim, no dirty-read hint."""
    return {
        "refId": ref,
        "datasource": DS,
        "editorMode": "code",
        "format": fmt,
        "rawQuery": True,
        "rawSql": sql,
    }


_kit = PanelKit(DS, target)

nid = _kit.nid
reset_id = _kit.reset_id
thresholds = _kit.thresholds
timeseries = _kit.timeseries
text_panel = _kit.text_panel
stat = _kit.stat
state_timeline = _kit.state_timeline
table = _kit.table
bargauge = _kit.bargauge
heatmap = _kit.heatmap
logs = _kit.logs
alertlist = _kit.alertlist
row = _kit.row
flow = _kit.flow
stat_grid = _kit.stat_grid
subtab = _kit.subtab
reflow = _kit.reflow


def server_var(multi: bool = False):
    """Build the $server template variable, from Darling's fleet registry.

    Single-select by default: every dashboard in this suite is a per-server investigation
    tool. Pass multi=True only where cross-server comparison is the dashboard's purpose
    (Fleet Overview) or a reporter-per-row design depends on it (Availability Groups).

    __text/__value so the label is the server name and panels filter on the indexed id.
    """
    return {
        "name": "server",
        "label": "Server",
        "type": "query",
        "datasource": DS,
        "query": (
            "SELECT name AS __text, server_id AS __value "
            "FROM config.config_monitored_servers "
            "WHERE is_enabled ORDER BY name"
        ),
        "current": {},
        "options": [],
        "refresh": 1,
        "hide": 0,
        "multi": multi,
        "includeAll": multi,
        "sort": 1,
        "description": "Monitored SQL Server instance, from Darling's fleet registry.",
    }


def query_var(
    name: str,
    label: str,
    query: str,
    description: str,
    multi: bool = True,
):
    """Build a query-backed template variable, defaulting to All."""
    return {
        "name": name,
        "label": label,
        "type": "query",
        "datasource": DS,
        "query": query,
        "definition": query,
        "current": {"text": ["All"], "value": ["$__all"]},
        "options": [],
        "refresh": 2,
        "hide": 0,
        "multi": multi,
        "includeAll": True,
        "sort": 0,
        "description": description,
    }


def single_query_var(name: str, label: str, query: str, description: str):
    """Build a single-select query-backed variable, auto-selecting the first row.

    Unlike query_var(), there is no "All" option - for a picker where exactly one value
    (e.g. one plan shape) must drive the panels below. refresh=2 (on time-range change)
    since the option list is itself time-bounded (only shapes seen in the current window).
    """
    return {
        "name": name,
        "label": label,
        "type": "query",
        "datasource": DS,
        "query": query,
        "definition": query,
        "current": {},
        "options": [],
        "refresh": 2,
        "hide": 0,
        "multi": False,
        "includeAll": False,
        "sort": 0,
        "description": description,
    }


def custom_var(name: str, label: str, options: list[str], description: str):
    """Build a single-select variable over a fixed option list, defaulting to the first."""
    return {
        "name": name,
        "label": label,
        "type": "custom",
        "query": ",".join(options),
        "current": {"text": options[0], "value": options[0]},
        "options": [
            {"text": o, "value": o, "selected": i == 0} for i, o in enumerate(options)
        ],
        "hide": 0,
        "multi": False,
        "includeAll": False,
        "description": description,
    }


_DASHBOARDS_DROPDOWN = {
    "asDropdown": True,
    "icon": "external link",
    "includeVars": True,
    "keepTime": True,
    "tags": ["perfmon-darling"],
    "targetBlank": False,
    "title": "All PerfMon (Darling) Dashboards",
    "type": "dashboards",
    "url": "",
}

_FINOPS_DROPDOWN = {
    "asDropdown": True,
    "icon": "external link",
    "includeVars": True,
    "keepTime": True,
    "tags": ["finops-darling"],
    "targetBlank": False,
    "title": "All FinOps (Darling) Dashboards",
    "type": "dashboards",
    "url": "",
}

_FLEET_LINK = {
    "title": "Fleet Overview",
    "icon": "dashboard",
    "type": "link",
    "url": "/d/darling-fleet?${__url_time_range}",
    "keepTime": True,
    "includeVars": False,
    "targetBlank": False,
}


def _dashboard_base(
    dash_uid, title, panels, variables, tags, links, time_from, refresh, graph_tooltip
):
    """Assemble the dashboard JSON envelope every Darling dashboard flavour shares."""
    return {
        "uid": dash_uid,
        "title": title,
        "tags": tags,
        "timezone": "",
        "schemaVersion": 39,
        "editable": True,
        "graphTooltip": graph_tooltip,
        "fiscalYearStartMonth": 0,
        "time": {"from": time_from, "to": "now"},
        "refresh": refresh,
        "weekStart": "",
        "annotations": {"list": []},
        "links": links,
        "templating": {"list": variables},
        "panels": panels,
    }


def dashboard(
    dash_uid,
    title,
    panels,
    variables,
    time_from="now-3h",
    refresh="1m",
    graph_tooltip=1,
):
    """Build a Darling dashboard envelope."""
    is_fleet = dash_uid == UID_PREFIX + "-fleet"
    links = [] if is_fleet else [_FLEET_LINK, _DASHBOARDS_DROPDOWN, _FINOPS_DROPDOWN]
    tags = ["perfmon-darling", "begin-here"] if is_fleet else ["perfmon-darling"]
    return _dashboard_base(
        dash_uid,
        f"Perfmon (Darling) - {title}",
        panels,
        variables,
        tags,
        links,
        time_from,
        refresh,
        graph_tooltip,
    )


def finops_dashboard(
    dash_uid, title, panels, variables, time_from="now-24h", refresh="5m"
):
    """Build a FinOps dashboard, one per upstream FinOps sub-tab."""
    return _dashboard_base(
        dash_uid,
        f"FinOps (Darling) - {title}",
        panels,
        variables,
        ["finops-darling"],
        [_FLEET_LINK, _FINOPS_DROPDOWN, _DASHBOARDS_DROPDOWN],
        time_from,
        refresh,
        1,
    )


def detail_dashboard(
    dash_uid, title, panels, variables, time_from="now-24h", prefix="Perfmon"
):
    """Build a drill-down dashboard reached from a data link."""
    return _dashboard_base(
        dash_uid,
        f"{prefix} (Darling) - {title}",
        panels,
        variables,
        ["perfmon-darling-detail", "nav-only"],
        [_FLEET_LINK, _DASHBOARDS_DROPDOWN],
        time_from,
        "",
        1,
    )


__all__ = [
    "DS",
    "DS_UID",
    "DURATION_STATUS_COLORS",
    "HEALTH_STATUS_COLORS",
    "OUT",
    "UTC_NOW",
    "alertlist",
    "bargauge",
    "col_datalink",
    "col_datalinks",
    "col_gauge_bar",
    "col_hidden",
    "col_thresholds",
    "col_unit",
    "collector",
    "custom_var",
    "dashboard",
    "detail_dashboard",
    "duration_ms",
    "finops_dashboard",
    "fixed",
    "flow",
    "heatmap",
    "logs",
    "n0",
    "multi_filter",
    "nid",
    "plan_parameters_sql",
    "query_var",
    "reflow",
    "reset_id",
    "rollup",
    "row",
    "series_style",
    "SERVER_REGISTRY",
    "server_filter",
    "server_join",
    "server_var",
    "single_query_var",
    "stat",
    "stat_grid",
    "state_timeline",
    "status_colors",
    "subtab",
    "table",
    "target",
    "text_panel",
    "text_var",
    "thresholds",
    "time_bucket",
    "uid",
    "UID_PREFIX",
    "timeseries",
]
