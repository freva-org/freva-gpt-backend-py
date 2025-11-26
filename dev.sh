#!/usr/bin/env bash
set -euo pipefail

# ------------------------------------------------------------------
# Simple dev launcher for freva-gpt-backend-py
#
# Custom flags (handled here, NOT passed to docker compose):
#   --debug / --DEBUG          -> DEBUG=1
#   --debug=0 / --DEBUG=0      -> DEBUG=0
#   --no-debug                 -> DEBUG=0
#
# Everything else is passed through to `docker compose`.
#
# Examples:
#   ./dev.sh up
#   ./dev.sh up --build -d
#   ./dev.sh --debug up --build -d
#   ./dev.sh up --build -d --debug
#
# IMPORTANT: A debug launcher (launch.json in VSCode) should be 
# configured to be able to use DEBUG mode.
# ------------------------------------------------------------------

# Set DEV flag for everything in this session
export DEV=1
export MCP_DISABLE_AUTH=1

DEBUG="${DEBUG:-0}"
COMPOSE_FILE="docker-compose.dev.yml"
COMPOSE_ARGS=()

for arg in "$@"; do
  case "$arg" in
    # Enable debug
    --debug|--DEBUG)
      DEBUG=1
      ;;
    # Explicit value: --debug=0 / --DEBUG=1 etc.
    --debug=*|--DEBUG=*)
      DEBUG="${arg#*=}"
      ;;
    # Disable debug
    --no-debug)
      DEBUG=0
      ;;
    # Help
    -h|--help)
      print_usage
      exit 0
      ;;
    # Everything else goes to docker compose
    *)
      COMPOSE_ARGS+=("$arg")
      ;;
  esac
done

# Export for docker compose / containers
export DEBUG

echo "[dev.sh] Using ${COMPOSE_FILE} with DEBUG=${DEBUG}"
echo "[dev.sh] docker compose -f ${COMPOSE_FILE} ${COMPOSE_ARGS[*]}"

docker compose -f "${COMPOSE_FILE}" "${COMPOSE_ARGS[@]}"