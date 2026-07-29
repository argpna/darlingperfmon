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
from darling_defs.cpu import cpu  # noqa: E402
from darling_defs.file_io import file_io  # noqa: E402
from darling_defs.latch_spinlock import latch_spinlock  # noqa: E402
from darling_defs.memory import memory  # noqa: E402
from darling_defs.perfmon import perfmon  # noqa: E402
from darling_defs.queries import queries  # noqa: E402
from darling_defs.session_stats import session_stats  # noqa: E402
from darling_defs.tempdb import tempdb  # noqa: E402
from darling_defs.waits import waits  # noqa: E402

DASHBOARDS = [
    cpu,
    file_io,
    latch_spinlock,
    memory,
    perfmon,
    queries,
    session_stats,
    tempdb,
    waits,
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
