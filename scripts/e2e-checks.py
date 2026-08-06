#!/usr/bin/env python3
"""Assert a provisioned Darling-line stack is wired up correctly, via the Grafana API.

Check:
  - the darling-fleet dashboard was imported
  - the Darling datasource exists and is a grafana-postgresql-datasource
  - a live query through it succeeds, proving the stored credentials
    authenticate against the store
  - at least one provisioned alert rule targets it

Environment variables:
  GRAFANA_API_KEY   Grafana service account token; read from ./.env when unset
  GRAFANA_URL       Grafana base URL (default: http://localhost:3000)
  DARLING_DS_UID    Darling datasource uid (default: darling)

Usage: python3 scripts/e2e-checks.py
"""

import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

GRAFANA = os.environ.get("GRAFANA_URL", "http://localhost:3000").rstrip("/")
DS_UID = os.environ.get("DARLING_DS_UID", "darling")

IDENTITY_SQL = "SELECT current_user AS who"


def _api_key() -> str:
    key = os.environ.get("GRAFANA_API_KEY")
    if not key:
        env_file = pathlib.Path(".env")
        if env_file.is_file():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                if line.startswith("GRAFANA_API_KEY="):
                    key = line.split("=", 1)[1].strip()
    if not key:
        sys.exit("GRAFANA_API_KEY not set and not found in ./.env")
    return key


def _request(key: str, path: str, payload: dict | None = None) -> dict | list:
    req = urllib.request.Request(
        f"{GRAFANA}{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def _check_fleet_dashboard(key: str) -> list[str]:
    try:
        dash = _request(key, "/api/dashboards/uid/darling-fleet")
    except urllib.error.HTTPError as err:
        return [f"darling-fleet: dashboard lookup failed ({err.code})"]
    if "panels" not in dash.get("dashboard", {}):
        return ["darling-fleet: dashboard has no panels"]
    print(f"  darling-fleet: imported, {len(dash['dashboard']['panels'])} panels")
    return []


def _check_datasource(key: str) -> list[str]:
    failures = []

    try:
        ds = _request(key, f"/api/datasources/uid/{DS_UID}")
    except urllib.error.HTTPError as err:
        return [f"{DS_UID}: datasource lookup failed ({err.code})"]

    want_type = "grafana-postgresql-datasource"
    got_type = ds.get("type", "")
    if got_type != want_type:
        failures.append(f"{DS_UID}: type {got_type!r}, expected {want_type!r}")

    query = {
        "queries": [
            {
                "refId": "A",
                "datasource": {"type": want_type, "uid": DS_UID},
                "rawSql": IDENTITY_SQL,
                "format": "table",
            }
        ],
        "from": "now-5m",
        "to": "now",
    }
    try:
        result = _request(key, "/api/ds/query", query)["results"]["A"]
    except urllib.error.HTTPError as err:
        result = json.load(err)["results"]["A"]
    if "error" in result:
        failures.append(f"{DS_UID}: live query failed: {result['error']}")
    else:
        who = result["frames"][0]["data"]["values"][0][0]
        print(f"  {DS_UID}: connects as {who}")

    return failures


def _check_alert_rules(key: str) -> list[str]:
    rules = _request(key, "/api/v1/provisioning/alert-rules")
    targeting = sum(
        1 for r in rules for q in r.get("data", []) if q.get("datasourceUid") == DS_UID
    )
    if targeting == 0:
        return [f"{DS_UID}: no provisioned alert rules target this datasource"]
    print(f"  {DS_UID}: {targeting} alert rule queries target it")
    return []


def main() -> None:
    key = _api_key()

    failures = _check_fleet_dashboard(key)
    failures += _check_datasource(key)
    failures += _check_alert_rules(key)

    for failure in failures:
        print(f"FAIL {failure}", file=sys.stderr)
    print(f"darling datasource {DS_UID!r} checked, {len(failures)} failure(s)")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
