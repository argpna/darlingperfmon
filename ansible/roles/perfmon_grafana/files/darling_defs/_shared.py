"""Shared helpers for the Darling (PostgreSQL) dashboard line.

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
    col_thresholds,
    col_unit,
    status_colors,
    text_var,
)

# Own directory and uid prefix so both dashboard lines can coexist in one Grafana.
_DASHBOARDS_ROOT = (
    pathlib.Path(__file__).resolve().parent.parent / "grafana" / "dashboards"
)
OUT = _DASHBOARDS_ROOT / "darling"

UID_PREFIX = "darling"


def uid(name: str) -> str:
    """Namespace a dashboard uid to the Darling line."""
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

# Timestamps are UTC in naive `timestamp` columns, so $__timeFilter() is correct as-is and
# no timezone helper is needed. The MSSQL line's tz_* helpers have no counterpart here.

# Retention tiers - a panel outrunning its table's horizon returns a short series, not an
# error. Retention: raw collectors ~30d, but query/procedure/query_store only 4d raw, with
# *_hourly at 21d and *_daily indefinite. Route with tiered() rather than picking by hand.
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

_UTC_NOW = "(now() AT TIME ZONE 'UTC')"


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
        clauses.append(f"$__timeFrom() < {_UTC_NOW} - INTERVAL '{lo} days'")
    if hi is not None:
        clauses.append(f"$__timeFrom() >= {_UTC_NOW} - INTERVAL '{hi} days'")
    return " AND ".join(clauses) if clauses else "true"


def tiered(branches: dict[str, str]) -> str:
    """Combine per-tier SELECTs into one query, guarded so exactly one tier runs.

    branches maps tier ("raw"/"hourly"/"daily") to a complete SELECT. Each is wrapped in its
    guard rather than the caller appending one, so a branch cannot escape routing and
    duplicate the result. An omitted tier's age range falls to the next coarser branch.

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
    parts = [
        f"SELECT * FROM (\n{sql.strip()}\n) AS {tier}_tier"
        f" WHERE {tier_guard(tier, present)}"
        for tier, sql in sorted(branches.items())
    ]
    return "\nUNION ALL\n".join(parts)


def server_filter(col: str = "server_id") -> str:
    """Filter to the instance(s) selected in $server.

    :csv, not :sqlstring - server_id is an integer, and quoting would fail the comparison.
    """
    return f"{col} IN (${{server:csv}})"


def time_bucket(interval: str, col: str = "collection_time") -> str:
    """TimescaleDB time_bucket() - takes arbitrary intervals, unlike date_trunc."""
    return f"time_bucket(INTERVAL '{interval}', {col})"


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
table = _kit.table
bargauge = _kit.bargauge
row = _kit.row
flow = _kit.flow
stat_grid = _kit.stat_grid
subtab = _kit.subtab
reflow = _kit.reflow


def server_var(multi: bool = True):
    """Build the $server template variable, from Darling's fleet registry.

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


_DASHBOARDS_DROPDOWN = {
    "asDropdown": True,
    "icon": "external link",
    "includeVars": True,
    "keepTime": True,
    "tags": ["perfmon"],
    "targetBlank": False,
    "title": "All PerfMon Dashboards",
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
    return {
        "uid": dash_uid,
        "title": title,
        "tags": ["perfmon", "begin-here"] if is_fleet else ["perfmon"],
        "timezone": "",
        "schemaVersion": 39,
        "editable": True,
        "graphTooltip": graph_tooltip,
        "fiscalYearStartMonth": 0,
        "time": {"from": time_from, "to": "now"},
        "refresh": refresh,
        "weekStart": "",
        "annotations": {"list": []},
        "links": [] if is_fleet else [_FLEET_LINK, _DASHBOARDS_DROPDOWN],
        "templating": {"list": variables},
        "panels": panels,
    }


__all__ = [
    "DS",
    "DS_UID",
    "DURATION_STATUS_COLORS",
    "HEALTH_STATUS_COLORS",
    "OUT",
    "bargauge",
    "col_datalink",
    "col_datalinks",
    "col_gauge_bar",
    "col_thresholds",
    "col_unit",
    "collector",
    "dashboard",
    "flow",
    "nid",
    "reflow",
    "reset_id",
    "rollup",
    "row",
    "server_filter",
    "server_var",
    "stat",
    "stat_grid",
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
