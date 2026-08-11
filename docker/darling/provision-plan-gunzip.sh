#!/bin/sh
# Enables plpython3u and a gunzip() UDF so Grafana's SQL-only panels can read plan XML the
# collector has stored gzip-compressed since v3.4.0/#2069.
# Upstream tracking issue: #2071
set -eu

: "${DARLING_PG_PASSWORD:?must be set}"

export PGPASSWORD="$DARLING_PG_PASSWORD"
PSQL="psql -h darling-pg -U darling -d darling -v ON_ERROR_STOP=1"

$PSQL -c "
CREATE EXTENSION IF NOT EXISTS plpython3u;

CREATE OR REPLACE FUNCTION public.darling_gunzip(data bytea) RETURNS text
LANGUAGE plpython3u IMMUTABLE STRICT AS \$\$
import gzip
return gzip.decompress(data).decode('utf-8')
\$\$;
"
echo "plpython3u enabled, public.darling_gunzip() available"
