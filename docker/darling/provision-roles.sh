#!/bin/sh
# Creates the least-privilege admin/viewer roles Grafana connects as.
set -eu

: "${PERFMON_VERSION:?must be set}"
: "${DARLING_PG_PASSWORD:?must be set}"
: "${DARLING_VIEWER_PASSWORD:?must be set}"

export PGPASSWORD="$DARLING_PG_PASSWORD"
PSQL="psql -h darling-pg -U darling -d darling -v ON_ERROR_STOP=1"

# provision-roles.sql needs the schemas the service creates on first start.
echo "waiting for the Darling service to migrate the store..."
until $PSQL -tAc "SELECT to_regclass('collect.wait_stats') IS NOT NULL" 2>/dev/null | grep -q '^t$'; do
    sleep 5
done

echo "waiting for the continuous aggregates..."
cagg_count() {
    $PSQL -tAc "SELECT count(*) FROM timescaledb_information.continuous_aggregates" 2>/dev/null || echo 0
}
last=-1
stable=0
while [ "$stable" -lt 4 ]; do
    sleep 5
    now=$(cagg_count)
    if [ "$now" -gt 0 ] && [ "$now" = "$last" ]; then
        stable=$((stable + 1))
    else
        stable=0
    fi
    last=$now
done

apk add --no-cache curl >/dev/null

url="https://raw.githubusercontent.com/erikdarlingdata/PerformanceMonitor/${PERFMON_VERSION}/Darling/tools/provision-roles.sql"
echo "fetching $url"
curl -fsSL --retry 5 --retry-delay 5 --retry-all-errors -o /tmp/provision-roles.sql "$url"

sed -e "s/CHANGE_ME_ADMIN_PASSWORD/${DARLING_PG_PASSWORD}/g" \
    -e "s/CHANGE_ME_VIEWER_PASSWORD/${DARLING_VIEWER_PASSWORD}/g" \
    /tmp/provision-roles.sql > /tmp/provision-roles.rendered.sql

attempts=60
until $PSQL -f /tmp/provision-roles.rendered.sql; do
    attempts=$((attempts - 1))
    if [ "$attempts" -le 0 ]; then
        echo "provision-roles.sql did not apply cleanly" >&2
        exit 1
    fi
    echo "grants not applicable yet, retrying in 5s..."
    sleep 5
done
echo "provisioned admin and viewer roles"
