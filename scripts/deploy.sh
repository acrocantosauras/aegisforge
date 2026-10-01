#!/bin/sh
# AegisForge production deployment helper (Phase 9).
#
# Usage:
#   ./scripts/deploy.sh check     # validate .env + compose config (no changes)
#   ./scripts/deploy.sh up        # build + start, wait for migrations + readiness
#   ./scripts/deploy.sh down      # stop (keeps volumes)
#   ./scripts/deploy.sh status    # service status
#   ./scripts/deploy.sh logs      # tail api + worker logs
#
# Include the observability overlay:  OBS=1 ./scripts/deploy.sh up
# Use an isolated compose project (fresh volumes):  PROJECT=aegisforge-prod ./scripts/deploy.sh up
# Use a different env file:            ENV_FILE=.env.prod ./scripts/deploy.sh up
set -eu

ENV_FILE="${ENV_FILE:-.env}"
COMPOSE="docker compose --env-file $ENV_FILE -f docker-compose.prod.yml"
if [ -n "${PROJECT:-}" ]; then
  COMPOSE="$COMPOSE -p $PROJECT"
fi
if [ "${OBS:-0}" = "1" ]; then
  COMPOSE="$COMPOSE -f docker-compose.observability.yml"
fi

fail() {
  echo "DEPLOY CHECK FAILED: $1" >&2
  exit 1
}

require_var() {
  key="$1"
  value=$(grep -E "^${key}=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- || true)
  [ -n "$value" ] || fail "$key must be set in .env"
  case "$value" in
    change-me*|"") fail "$key still has a placeholder value" ;;
  esac
}

cmd_check() {
  [ -f "$ENV_FILE" ] || fail "$ENV_FILE not found (cp .env.example $ENV_FILE)"
  for v in SECRET_KEY POSTGRES_PASSWORD REDIS_PASSWORD CORS_ORIGINS \
           API_DOMAIN APP_DOMAIN ACME_EMAIL NEXT_PUBLIC_API_URL; do
    require_var "$v"
  done
  # SECRET_KEY must be strong enough for production fail-fast (>= 32 chars).
  sk=$(grep -E "^SECRET_KEY=" "$ENV_FILE" | tail -1 | cut -d= -f2-)
  [ "${#sk}" -ge 32 ] || fail "SECRET_KEY must be >= 32 characters"
  echo "Deploy check OK (env + required secrets present)."
  $COMPOSE config >/dev/null && echo "Compose config valid."
}

cmd_up() {
  cmd_check
  echo "Building and starting production stack..."
  $COMPOSE up -d --build
  echo "Waiting for API readiness (migrations run inside api-migrate first)..."
  i=0
  until $COMPOSE ps api | grep -q healthy; do
    i=$((i + 1))
    [ "$i" -ge 60 ] && $COMPOSE logs --tail 50 api api-migrate && fail "API did not become healthy"
    sleep 5
  done
  echo "Stack is up. Services:"
  $COMPOSE ps
}

cmd_down() {
  $COMPOSE down
}

cmd_status() {
  $COMPOSE ps
}

cmd_logs() {
  $COMPOSE logs -f --tail 100 api worker-1 worker-2
}

case "${1:-}" in
  check)  cmd_check ;;
  up)     cmd_up ;;
  down)   cmd_down ;;
  status) cmd_status ;;
  logs)   cmd_logs ;;
  *) echo "Usage: $0 {check|up|down|status|logs}" >&2; exit 2 ;;
esac
