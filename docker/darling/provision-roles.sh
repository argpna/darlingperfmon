#!/bin/sh
# Creates the least-privilege admin/viewer roles Grafana connects as. Demo-stack only:
# a real store is owned by its operator, and the Ansible role never does this.
# Fetched from the upstream tag rather than vendored so it cannot drift.
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

if $PSQL -tAc "SELECT 1 FROM pg_roles WHERE rolname='viewer'" | grep -q '^1$'; then
    echo "viewer role already present, nothing to do"
    exit 0
fi

url="https://raw.githubusercontent.com/erikdarlingdata/PerformanceMonitor/${PERFMON_VERSION}/Darling/tools/provision-roles.sql"
echo "fetching $url"
wget -q -O /tmp/provision-roles.sql "$url"

sed -e "s/CHANGE_ME_ADMIN_PASSWORD/${DARLING_PG_PASSWORD}/g" \
    -e "s/CHANGE_ME_VIEWER_PASSWORD/${DARLING_VIEWER_PASSWORD}/g" \
    /tmp/provision-roles.sql > /tmp/provision-roles.rendered.sql

$PSQL -f /tmp/provision-roles.rendered.sql
echo "provisioned admin and viewer roles"
