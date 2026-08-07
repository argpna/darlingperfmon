#!/usr/bin/env python3
"""Smoke-test every panel query in every dashboard through the Grafana query API.

Derives template-variable substitutions from each dashboard's own variable list,
walks panels recursively. Handles both classic v1 dashboard JSON and
dashboard.grafana.app/v2 resources.

Environment variables:
  GRAFANA_API_KEY         Grafana service account token (preferred)
  GRAFANA_ADMIN_PASSWORD  basic-auth password for the admin user (alternative)
  GRAFANA_URL             Grafana base URL (default: http://localhost:3000)
  PERFMON_DASHBOARD_DIR   directory containing dashboard JSON files

Usage: python3 scripts/verify-panels.py <datasource-uid> [<datasource-uid> ...]
"""

import base64
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request

GRAFANA = os.environ.get("GRAFANA_URL", "http://localhost:3000").rstrip("/")
_api_key = os.environ.get("GRAFANA_API_KEY")
_admin_password = os.environ.get("GRAFANA_ADMIN_PASSWORD")
if _api_key:
    AUTH_HEADER = f"Bearer {_api_key}"
elif _admin_password:
    AUTH_HEADER = (
        "Basic " + base64.b64encode(f"admin:{_admin_password}".encode()).decode()
    )
else:
    sys.exit("Set GRAFANA_API_KEY or GRAFANA_ADMIN_PASSWORD")
if len(sys.argv) < 2:
    sys.exit("Usage: verify-panels.py <datasource-uid> [<datasource-uid> ...]")
DS_UIDS = sys.argv[1:]

REQUEST_TIMEOUT_SECONDS = 30

RETRY_ATTEMPTS = 4
RETRY_DELAY_SECONDS = 15

NON_SQL_VARIABLE_TYPES = {"datasource"}
NON_SQL_VARIABLE_KINDS = {"DatasourceVariable"}

# Grafana's reserved "All selected" sentinel for multi-value variables.
ALL_VARIABLE_VALUE = "$__all"

# Escape hatch: only needed when a variable's own `current.value` in the dashboard JSON
# isn't a good smoke-test value e.g. would trivially return 0 rows for a reason that
# masks a real bug. Empty by design - prefer fixing the dashboard's default instead of
# adding an entry here.
SUBS_OVERRIDES: dict[str, str] = {}


def _escape_sql_string(value: str) -> str:
    """Reproduce T-SQL single-quote doubling. Bare ${var} does not add surrounding
    quotes (the SQL text supplies its own); only :sqlstring does - keep these as
    separate substitutions rather than collapsing both into one pre-quoted form,
    or a bare ${var} call site would get double-quoted and silently mask errors."""
    return str(value).replace("'", "''")


def _sqlstring(value: str) -> str:
    """Reproduce Grafana's ${var:sqlstring} formatting: quote and escape for T-SQL."""
    return "'" + _escape_sql_string(value) + "'"


def _is_v2_dashboard(dash: dict) -> bool:
    """True for a dashboard.grafana.app/v2* resource (kind/spec shape), False for
    classic v1 dashboard JSON."""
    return dash.get("kind") == "Dashboard" and "spec" in dash


def _dashboard_subs(dash: dict, ds_uid: str | None = None) -> dict[str, str]:
    """Build the ${name} -> escaped-only value table for one dashboard, derived from
    its own variable list (v1 templating.list or v2 spec.variables) instead of a
    hand-maintained global dict."""
    if _is_v2_dashboard(dash):
        variables = [
            (v.get("kind"), v.get("spec", {}))
            for v in dash["spec"].get("variables", [])
        ]
    else:
        variables = [
            (v.get("type"), v) for v in dash.get("templating", {}).get("list", [])
        ]
    subs = {}
    for kind, var in variables:
        name = var.get("name")
        if not name or kind in NON_SQL_VARIABLE_TYPES | NON_SQL_VARIABLE_KINDS:
            continue
        if name in SUBS_OVERRIDES:
            value = SUBS_OVERRIDES[name]
        else:
            current = var.get("current", {})
            value = current.get("value", "")
            if isinstance(value, list):
                value = value[0] if value else ""
            if value == ALL_VARIABLE_VALUE:
                value = var.get("allValue") or ""
            if not value and kind == "query" and ds_uid:
                value = _resolve_query_var(var, ds_uid)
        subs[name] = str(value)
    return subs


def _resolve_query_var(var: dict, ds_uid: str) -> str:
    """Resolve a query-backed variable by running its query, the way Grafana does.

    Their options populate at dashboard load, so `current` is empty in the generated JSON
    and panels filtering on one would otherwise be tested with an empty token.
    """
    query = var.get("query")
    if isinstance(query, dict):
        query = query.get("rawSql") or query.get("query")
    if not isinstance(query, str) or not query.strip():
        return ""
    return _first_variable_value(ds_uid, query)


def _first_variable_value(ds_uid: str, query: str) -> str:
    """Return the first __value (or first column) the variable query yields."""
    body = json.dumps(
        {
            "queries": [
                {
                    "refId": "A",
                    "datasource": {"type": ds_type(ds_uid), "uid": ds_uid},
                    "format": "table",
                    "rawQuery": True,
                    "rawSql": query,
                    "intervalMs": 60000,
                    "maxDataPoints": 500,
                }
            ],
            "from": "now-3h",
            "to": "now",
        }
    ).encode()
    req = urllib.request.Request(
        f"{GRAFANA}/api/ds/query",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": AUTH_HEADER},
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            r = json.load(resp)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        print(f"  (could not resolve variable query: {e})")
        return ""
    frames = r.get("results", {}).get("A", {}).get("frames", [])
    for fr in frames:
        fields = [f.get("name") for f in fr.get("schema", {}).get("fields", [])]
        values = fr.get("data", {}).get("values", [])
        if not values or not values[0]:
            continue
        idx = fields.index("__value") if "__value" in fields else len(values) - 1
        return str(values[idx][0])
    return ""


def _apply_subs(sql: str, subs: dict[str, str]) -> str:
    for name, value in subs.items():
        sql = sql.replace(f"${{{name}:sqlstring}}", _sqlstring(value))
        sql = sql.replace(f"${{{name}:csv}}", _escape_sql_string(value))
        sql = sql.replace(f"${{{name}}}", _escape_sql_string(value))
    return sql


def iter_panels(panels):
    """Yield every panel, recursing into collapsed rows' nested "panels" list so those
    targets aren't silently skipped."""
    for panel in panels:
        yield panel
        if panel.get("type") == "row" and panel.get("panels"):
            yield from iter_panels(panel["panels"])


def _is_text_only(dash: dict) -> bool:
    """True when every panel is a text panel (e.g. a dashboard documenting an
    upstream feature that cannot run as a Grafana query), so zero extractable
    queries is legitimate rather than a walker blind spot."""
    if _is_v2_dashboard(dash):
        elements = dash["spec"].get("elements", {}).values()
        groups = [e.get("spec", {}).get("vizConfig", {}).get("group") for e in elements]
        return bool(groups) and all(g == "text" for g in groups)
    panels = [p for p in iter_panels(dash.get("panels", [])) if p.get("type") != "row"]
    return bool(panels) and all(p.get("type") == "text" for p in panels)


def iter_queries(dash: dict):
    """Yield (panel_title, format, rawSql) for every SQL query in a dashboard,
    handling both classic v1 JSON and dashboard.grafana.app/v2* resources."""
    if _is_v2_dashboard(dash):
        for element in dash["spec"].get("elements", {}).values():
            panel_spec = element.get("spec", {})
            queries = panel_spec.get("data", {}).get("spec", {}).get("queries", [])
            for q in queries:
                query_spec = q.get("spec", {}).get("query", {}).get("spec", {})
                sql = query_spec.get("rawSql")
                if sql:
                    yield (
                        panel_spec.get("title", "(untitled)"),
                        query_spec.get("format", "table"),
                        sql,
                    )
        return
    for panel in iter_panels(dash.get("panels", [])):
        for t in panel.get("targets", []):
            sql = t.get("rawSql")
            if sql:
                yield (
                    panel.get("title", "(untitled)"),
                    t.get("format", "table"),
                    sql,
                )


_DS_TYPES: dict[str, str] = {}


def ds_type(ds_uid: str) -> str:
    """Look up a datasource's plugin type, cached per uid."""
    if ds_uid not in _DS_TYPES:
        req = urllib.request.Request(
            f"{GRAFANA}/api/datasources/uid/{ds_uid}",
            headers={"Authorization": AUTH_HEADER},
        )
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
                _DS_TYPES[ds_uid] = json.load(resp).get("type", "mssql")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            _DS_TYPES[ds_uid] = "mssql"
    return _DS_TYPES[ds_uid]


def run_query(ds_uid: str, sql: str, fmt: str):
    body = json.dumps(
        {
            "queries": [
                {
                    "refId": "A",
                    "datasource": {"type": ds_type(ds_uid), "uid": ds_uid},
                    "format": fmt,
                    "rawQuery": True,
                    "rawSql": sql,
                    "intervalMs": 60000,
                    "maxDataPoints": 500,
                }
            ],
            "from": "now-3h",
            "to": "now",
        }
    ).encode()
    req = urllib.request.Request(
        f"{GRAFANA}/api/ds/query",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": AUTH_HEADER,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            r = json.load(resp)
    except urllib.error.HTTPError as e:
        try:
            r = json.load(e)
        except json.JSONDecodeError:
            return ("ERROR", f"HTTP {e.code}: {e.reason}")
    except (urllib.error.URLError, TimeoutError) as e:
        return ("ERROR", f"request failed: {e}")

    a = r.get("results", {}).get("A")
    if a is None:
        return ("ERROR", f"no result for query A: {r}")
    if a.get("error"):
        return ("ERROR", a["error"])
    rows = 0
    for fr in a.get("frames", []):
        vals = fr["data"]["values"]
        rows += len(vals[0]) if vals else 0
    return ("OK", rows)


def run_query_with_retry(ds_uid: str, sql: str, fmt: str):
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        status, detail = run_query(ds_uid, sql, fmt)
        if status == "OK" or attempt == RETRY_ATTEMPTS:
            return status, detail
        time.sleep(RETRY_DELAY_SECONDS)
    return status, detail


def load_dashboard(path: pathlib.Path) -> dict | None:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError) as e:
        print(f"{path.name:28s} | <<< COULD NOT PARSE: {e}")
        return None


def main() -> None:
    root = pathlib.Path(__file__).resolve().parent.parent
    _default_dir = (
        root
        / "ansible"
        / "roles"
        / "perfmon_grafana"
        / "files"
        / "grafana"
        / "dashboards"
        / "darling"
    )
    dashboard_dir = (
        pathlib.Path(os.environ["PERFMON_DASHBOARD_DIR"])
        if "PERFMON_DASHBOARD_DIR" in os.environ
        else _default_dir
    )
    failures = 0
    tested = 0
    for f in sorted(dashboard_dir.rglob("*.json")):
        dash = load_dashboard(f)
        if dash is None:
            failures += 1
            continue
        name = f.name
        subs = _dashboard_subs(dash, DS_UIDS[0])
        extracted = 0
        for title, fmt, sql in iter_queries(dash):
            extracted += 1
            sql = _apply_subs(sql, subs)
            for ds_uid in DS_UIDS:
                tested += 1
                status, detail = run_query_with_retry(ds_uid, sql, fmt)
                flag = ""
                if status == "ERROR":
                    failures += 1
                    flag = "  <<< FAIL"
                    detail = str(detail)[:160]
                prefix = f"{name:28s} | {title[:52]:52s}"
                suffix = f"[{ds_uid}]" if len(DS_UIDS) > 1 else ""
                print(f"{prefix} | {status} {detail}{flag} {suffix}")
        if extracted == 0:
            if _is_text_only(dash):
                print(f"{name:28s} | text-only dashboard, no queries to test")
            else:
                failures += 1
                print(
                    f"{name:28s} | <<< NO EXTRACTABLE QUERIES - unknown dashboard shape?"
                )
    print(f"\n{tested} panel queries checked across {len(DS_UIDS)} datasource(s)")
    print(f"{f'FAILURES: {failures}' if failures else 'ALL PANEL QUERIES OK'}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
