"""Running Jobs dashboard (Darling line).

Upstream ref: ViewerServerTab.RunningJobs.cs, ViewerDataService.RunningJobs.cs. Single
flat tab: latest snapshot of running SQL Agent jobs, plus msdb-access status derived from
the running_jobs collector's own run outcome (the viewer has no live msdb probe).

Only the 8 columns upstream's grid actually displays are shown. Durations use Grafana's
native `s` unit instead of the C#'s formatted strings, so they stay numerically sortable.
"""

from ._shared import (
    col_unit,
    collector,
    dashboard,
    flow,
    reset_id,
    server_filter,
    server_join,
    server_var,
    status_colors,
    table,
    uid,
)

# Upstream ref: ShouldShowMsdbBanner - PERMISSIONS status only, not any-error.
_MSDB_STATUS_SQL = f"""
SELECT DISTINCT ON (cl.server_id)
    srv.name AS "Server",
    cl.status AS "Status"
FROM {collector('collection_log')} AS cl
{server_join('cl.server_id')}
WHERE cl.collector_name = 'running_jobs'
  AND {server_filter('cl.server_id')}
ORDER BY cl.server_id, cl.collection_time DESC
"""

# Upstream ref: RunningJobsSql, extended to a per-server latest snapshot for multi-select $server.
_RUNNING_JOBS_SQL = f"""
WITH latest AS (
    SELECT
        rj.server_id,
        srv.name AS server_label,
        MAX(rj.collection_time) AS mx
    FROM {collector('running_jobs')} AS rj
    {server_join('rj.server_id')}
    WHERE $__timeFilter(rj.collection_time)
      AND {server_filter('rj.server_id')}
    GROUP BY rj.server_id, srv.name
)
SELECT
    l.server_label AS "Server",
    rj.job_name AS "Job Name",
    rj.start_time AS "Start Time",
    rj.current_duration_seconds AS "Current Duration",
    rj.avg_duration_seconds AS "Avg Duration",
    rj.p95_duration_seconds AS "P95 Duration",
    rj.percent_of_average AS "% of Average",
    CASE WHEN rj.is_running_long THEN 'Yes' ELSE '' END AS "Running Long",
    rj.successful_run_count AS "Successful Runs (30d)"
FROM latest AS l
JOIN {collector('running_jobs')} AS rj
  ON rj.server_id = l.server_id
 AND rj.collection_time = l.mx
ORDER BY rj.current_duration_seconds DESC
"""


def running_jobs():
    """Build the Running Jobs dashboard."""
    reset_id()
    panels: list[dict] = []

    flow(
        panels,
        0,
        [
            (
                24,
                5,
                lambda x, y, w, h: table(
                    "Msdb Access Status",
                    x,
                    y,
                    w,
                    h,
                    _MSDB_STATUS_SQL,
                    overrides=[
                        status_colors(
                            "Status",
                            {
                                "SUCCESS": "green",
                                "PERMISSIONS": "purple",
                                "ERROR": "red",
                                "YIELDED": "yellow",
                                "SKIPPED": "text",
                            },
                        ),
                    ],
                    description="PERMISSIONS means the login lacks msdb access.",
                ),
            ),
            (
                24,
                13,
                lambda x, y, w, h: table(
                    "Currently Running SQL Agent Jobs",
                    x,
                    y,
                    w,
                    h,
                    _RUNNING_JOBS_SQL,
                    sort_by=[{"displayName": "Current Duration", "desc": True}],
                    overrides=[
                        status_colors("Running Long", {"Yes": "red"}),
                        col_unit("Current Duration", "s"),
                        col_unit("Avg Duration", "s"),
                        col_unit("P95 Duration", "s"),
                        col_unit("% of Average", "percent"),
                    ],
                ),
            ),
        ],
    )

    return dashboard(uid("running-jobs"), "Running Jobs", panels, [server_var()])
