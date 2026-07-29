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
# error. Anything over 4 days on the query tables must read a continuous aggregate.
#
#   raw collector tables       ~30 days
#   query/procedure/query_store 4 days raw
#   *_hourly                   21 days
#   *_baseline                 35 days
#   *_daily                    indefinite
#
# Upstream's range-to-source routing: Darling.Service/Compose/ComposeSourceRouter.cs.

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

_ROLLUP_TIERS = ("hourly", "daily", "baseline")


def collector(base: str) -> str:
    """Resolve the relation for a raw collector table, preferring its v_* view."""
    return f"collect.{base}" if base in _NO_VIEW else f"collect.v_{base}"


def rollup(base: str, tier: str) -> str:
    """Resolve a continuous aggregate, for ranges beyond the raw table's retention."""
    if tier not in _ROLLUP_TIERS:
        raise ValueError(
            f"unknown rollup tier {tier!r}, expected one of {_ROLLUP_TIERS}"
        )
    return f"collect.{base}_{tier}"


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
