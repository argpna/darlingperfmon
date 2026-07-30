#!/usr/bin/env python3
"""Generate the Darling (PostgreSQL) PerformanceMonitor Grafana dashboards.

Per-dashboard modules live in darling_defs/, shared helpers in darling_defs/_shared.py,
panel builders in panel_kit.py. The MSSQL line is build-dashboards.py + dashboard_defs/.

Usage:
  python3 build-darling-dashboards.py
  python3 build-darling-dashboards.py --output /custom/path
"""

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
# pylint: disable=wrong-import-position
from darling_defs._shared import OUT  # noqa: E402
from darling_defs.availability_groups import (  # noqa: E402
    availability_group_detail,
    availability_groups,
)
from darling_defs.blocking import blocking, deadlock_detail  # noqa: E402
from darling_defs.collection_health import collection_health  # noqa: E402
from darling_defs.config_changes import config_changes  # noqa: E402
from darling_defs.configuration import configuration  # noqa: E402
from darling_defs.cpu import cpu  # noqa: E402
from darling_defs.daily_summary import daily_summary  # noqa: E402
from darling_defs.file_io import file_io  # noqa: E402
from darling_defs.fleet import fleet  # noqa: E402
from darling_defs.latch_spinlock import latch_spinlock  # noqa: E402
from darling_defs.memory import memory  # noqa: E402
from darling_defs.perfmon import perfmon  # noqa: E402
from darling_defs.queries import queries  # noqa: E402
from darling_defs.session_stats import session_stats  # noqa: E402
from darling_defs.tempdb import tempdb  # noqa: E402
from darling_defs.waits import waits  # noqa: E402
from darling_defs.finops.application_connections import (  # noqa: E402
    application_connections,
)
from darling_defs.finops.database_resources import database_resources  # noqa: E402
from darling_defs.finops.database_sizes import database_sizes  # noqa: E402
from darling_defs.finops.high_impact import high_impact  # noqa: E402
from darling_defs.finops.index_analysis import index_analysis  # noqa: E402
from darling_defs.finops.index_usage import index_usage  # noqa: E402
from darling_defs.finops.locking import locking  # noqa: E402
from darling_defs.finops.object_sizes import object_sizes  # noqa: E402
from darling_defs.finops.optimization import optimization  # noqa: E402
from darling_defs.finops.recommendations import recommendations  # noqa: E402
from darling_defs.finops.server_inventory import server_inventory  # noqa: E402
from darling_defs.finops.storage_growth import storage_growth  # noqa: E402
from darling_defs.finops.utilization import utilization  # noqa: E402

DASHBOARDS = [
    availability_groups,
    availability_group_detail,
    blocking,
    deadlock_detail,
    collection_health,
    config_changes,
    configuration,
    cpu,
    daily_summary,
    file_io,
    fleet,
    latch_spinlock,
    memory,
    perfmon,
    queries,
    session_stats,
    tempdb,
    waits,
    # FinOps: one dashboard per upstream FinOps sub-tab, plus the two Storage Growth
    # drill-down levels (object_sizes, index_usage) reached from its data links.
    application_connections,
    database_resources,
    database_sizes,
    high_impact,
    index_analysis,
    index_usage,
    locking,
    object_sizes,
    optimization,
    recommendations,
    server_inventory,
    storage_growth,
    utilization,
]


def main() -> None:
    """Build every Darling dashboard and write it to the output directory."""
    parser = argparse.ArgumentParser(
        description="Generate the Darling PerformanceMonitor Grafana dashboards."
    )
    parser.add_argument(
        "--output",
        metavar="DIR",
        help=(
            "Root directory to write dashboard JSON into. Dashboards are written to "
            "<DIR>/darling/. Defaults to grafana/dashboards/ relative to this script."
        ),
    )
    args = parser.parse_args()

    out = (
        pathlib.Path(args.output).expanduser().resolve() / OUT.name
        if args.output
        else OUT
    )
    out.mkdir(parents=True, exist_ok=True)

    for build in DASHBOARDS:
        d = build()
        path = out / f"{d['uid']}.json"
        path.write_text(json.dumps(d, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
