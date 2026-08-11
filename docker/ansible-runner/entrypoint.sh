#!/usr/bin/env bash
# Bootstraps the Grafana API key then runs the main Ansible playbook.
# GRAFANA_URL, GRAFANA_ADMIN_USER, and GRAFANA_ADMIN_PASSWORD must be
# available in the environment (set via docker-compose).
set -euo pipefail

echo "Bootstrapping Grafana API key..."
GRAFANA_API_KEY=$(/workspace/bootstrap/setup-grafana-api-key.sh)
export GRAFANA_API_KEY

if [ "$#" -gt 0 ]; then
  exec "$@"
fi

exec ansible-playbook \
  -i /workspace/ansible/inventory/docker \
  /workspace/ansible/playbooks/main.yml
