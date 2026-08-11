#!/bin/sh
# Enables collectors that default OFF fleet-wide with no config.config_collector_schedules
# row (e.g. long_query_completions - see darling_defs/queries.py's _TRACE_STATUS_SQL).
set -eu

: "${DARLING_PG_PASSWORD:?must be set}"

export PGPASSWORD="$DARLING_PG_PASSWORD"
PSQL="psql -h darling-pg -U darling -d darling -v ON_ERROR_STOP=1"

echo "waiting for the Darling service to migrate the store..."
until $PSQL -tAc "SELECT to_regclass('config.config_collector_schedules') IS NOT NULL" 2>/dev/null | grep -q '^t$'; do
    sleep 5
done

$PSQL -c "
INSERT INTO config.config_collector_schedules (server_id, collector_name, enabled)
VALUES (NULL, 'long_query_completions', TRUE)
ON CONFLICT (collector_name) WHERE server_id IS NULL
DO UPDATE SET enabled = EXCLUDED.enabled;
"
echo "long_query_completions enabled fleet-wide"
