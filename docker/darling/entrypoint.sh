#!/bin/sh
# Render darling.json from the template so demo passwords come from .env.
# Plaintext passwords are upstream's dev-only path; the service warns on every use.
set -eu

: "${MSSQL_SA_PASSWORD:?must be set}"
: "${DARLING_PG_PASSWORD:?must be set}"

envsubst < /config/darling.json.template > /tmp/darling.json
chmod 600 /tmp/darling.json
export DARLING_CONFIG=/tmp/darling.json

exec dotnet PerformanceMonitor.Darling.Service.dll "$@"
