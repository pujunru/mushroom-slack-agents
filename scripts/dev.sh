#!/usr/bin/env bash
# Convenience wrapper for the local stack.
#
#   ./scripts/dev.sh up        start everything
#   ./scripts/dev.sh down      stop and keep data
#   ./scripts/dev.sh reset     stop and delete data
#   ./scripts/dev.sh logs      follow gateway and agent logs
#   ./scripts/dev.sh test      run the unit suite
#   ./scripts/dev.sh itest     run the pgmq integration suite against the stack
#   ./scripts/dev.sh psql      open a shell on the database
#   ./scripts/dev.sh send      post a signed fake Slack event to the gateway

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE=(docker compose -f "${ROOT}/deploy/local/docker-compose.yml")
DSN="postgresql://mushroom:mushroom@localhost:5432/mushroom"

case "${1:-up}" in
  up)
    "${COMPOSE[@]}" up --build -d
    echo
    echo "Gateway via Traefik:  http://localhost:8080"
    echo "Traefik dashboard:    http://localhost:8081"
    echo "Stats:                curl http://localhost:8080/stats"
    ;;
  down)   "${COMPOSE[@]}" down ;;
  reset)  "${COMPOSE[@]}" down -v ;;
  logs)   "${COMPOSE[@]}" logs -f gateway agent ;;
  psql)   "${COMPOSE[@]}" exec postgres psql -U mushroom -d mushroom ;;
  test)
    cd "${ROOT}"
    python -m pytest -q
    ;;
  itest)
    cd "${ROOT}"
    MUSHROOM_TEST_DATABASE_URL="${DSN}" python -m pytest tests/test_queue_integration.py -v
    ;;
  send)
    # Posts a signed app_mention so the full path can be exercised without
    # wiring up Slack.
    "${ROOT}/scripts/send-test-event.py" "${2:-hello from the test script}"
    ;;
  *)
    echo "usage: $0 {up|down|reset|logs|psql|test|itest|send}" >&2
    exit 1
    ;;
esac
