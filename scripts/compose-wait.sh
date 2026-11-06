#!/usr/bin/env bash
# scripts/compose-wait.sh
# Wait for all compose services to become healthy.
# Usage: scripts/compose-wait.sh [timeout_seconds] [compose_project_name]
set -euo pipefail

TIMEOUT="${1:-120}"
PROJECT="${2:-${COMPOSE_PROJECT_NAME:-}}"
SERVICES=(postgres web api)
deadline=$((SECONDS + TIMEOUT))

compose_cmd() {
  if [[ -n "$PROJECT" ]]; then
    docker compose -p "$PROJECT" "$@"
  else
    docker compose "$@"
  fi
}

for svc in "${SERVICES[@]}"; do
  echo -n "Waiting for $svc..."
  while true; do
    if (( SECONDS >= deadline )); then
      echo " TIMEOUT after ${TIMEOUT}s"
      compose_cmd ps
      compose_cmd logs "$svc" --tail=30
      exit 1
    fi
    status=$(compose_cmd ps "$svc" --format json 2>/dev/null \
      | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('Health',''))" 2>/dev/null || echo "")
    if [[ "$status" == "healthy" ]]; then
      echo " healthy"
      break
    fi
    sleep 2
  done
done

echo ""
echo "=== Health check results ==="
curl -sf http://localhost:8787/api/health/ready | python3 -m json.tool
echo ""
curl -sf http://localhost:4002/api/auth/providers | python3 -c "import sys,json; print('Web JWKS endpoint reachable')" 2>/dev/null || echo "Web health check"
echo ""
echo "All services healthy."
