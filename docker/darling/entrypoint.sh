#!/bin/sh
# darling.json is rendered by the perfmon_darling Ansible role onto a shared volume;
# wait for the first ansible-runner run to write it.
set -eu

CONFIG=/etc/darling/darling.json

echo "waiting for $CONFIG..."
until [ -f "$CONFIG" ]; do
    sleep 5
done

export DARLING_CONFIG="$CONFIG"
exec dotnet PerformanceMonitor.Darling.Service.dll "$@"
