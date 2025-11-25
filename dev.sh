#!/usr/bin/env bash
set -e

# Set DEV flag for everything in this session
export DEV=1
export MCP_DISABLE_AUTH=1

# Select compose files for development
export COMPOSE_FILE="docker-compose.dev.yml"

# Start Docker Compose normally
docker compose "$@"