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
from darling_defs.administration import administration  # noqa: E402
from darling_defs.availability_groups import (  # noqa: E402
    availability_group_detail,
    availability_groups,
)
from darling_defs.blocking import blocking, deadlock_detail  # noqa: E402
from darling_defs.collection_health import (  # noqa: E402
    collection_health,
    collection_log_detail,
)
from darling_defs.cpu_memory_sessions import cpu_memory_sessions  # noqa: E402
from darling_defs.fleet import fleet  # noqa: E402
from darling_defs.overview import overview  # noqa: E402
from darling_defs.queries import (  # noqa: E402
    procedure_history,
    queries,
    query_stats_history,
    query_store_history,
)
from darling_defs.storage_tempdb import storage_tempdb  # noqa: E402
from darling_defs.system_events import system_events  # noqa: E402
from darling_defs.wait_analysis import wait_analysis  # noqa: E402
from darling_defs.waits import wait_drill_down  # noqa: E402
from darling_defs.finops.capacity_growth import capacity_growth  # noqa: E402
from darling_defs.finops.index_usage import index_usage  # noqa: E402
from darling_defs.finops.object_sizes import object_sizes  # noqa: E402
from darling_defs.finops.optimization_indexing import (  # noqa: E402
    optimization_indexing,
)
from darling_defs.finops.recommendations import recommendations  # noqa: E402
from darling_defs.finops.server_inventory import server_inventory  # noqa: E402
from darling_defs.finops.utilization import utilization  # noqa: E402
from darling_defs.finops.workload_contention import workload_contention  # noqa: E402

DASHBOARDS = [
    administration,
    availability_groups,
    availability_group_detail,
    blocking,
    deadlock_detail,
    collection_health,
    collection_log_detail,
    cpu_memory_sessions,
    fleet,
    overview,
    procedure_history,
    queries,
    query_stats_history,
    query_store_history,
    storage_tempdb,
    system_events,
    wait_analysis,
    wait_drill_down,
    # FinOps: Recommendations stays the unchanged landing page; the rest are consolidated
    # groups, plus the two Storage Growth drill-down levels (object_sizes, index_usage)
    # reached from its data links.
    capacity_growth,
    index_usage,
    object_sizes,
    optimization_indexing,
    recommendations,
    server_inventory,
    utilization,
    workload_contention,
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
