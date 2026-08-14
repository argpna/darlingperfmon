#!/bin/sh
# darling.json is rendered by the perfmon_darling Ansible role onto a shared volume; wait for
# it to appear, then watch it for later changes and restart the collector process (not the
# container) when it does. A newly onboarded SQL-auth instance's credentials only take effect
# on the collector's next start (see perfmon_darling's README, "Restarting the collector"), and
# this container has nothing else watching for that.
set -u

CONFIG=/etc/darling/darling.json

echo "waiting for $CONFIG..."
until [ -f "$CONFIG" ]; do
    sleep 5
done

export DARLING_CONFIG="$CONFIG"

config_hash() {
    sha256sum "$CONFIG" | cut -d' ' -f1
}

child_pid=""
on_term() {
    [ -n "$child_pid" ] && kill -TERM "$child_pid" 2>/dev/null
    wait "$child_pid" 2>/dev/null
    exit 0
}
trap on_term TERM INT

while true; do
    hash_before="$(config_hash)"
    dotnet PerformanceMonitor.Darling.Service.dll "$@" &
    child_pid=$!
    restart_requested=0

    while kill -0 "$child_pid" 2>/dev/null; do
        sleep 10
        if [ "$(config_hash)" != "$hash_before" ]; then
            echo "darling.json changed, restarting the collector..."
            restart_requested=1
            kill -TERM "$child_pid" 2>/dev/null
            break
        fi
    done

    wait "$child_pid"
    exit_code=$?
    [ "$restart_requested" -eq 1 ] || exit "$exit_code"
done
