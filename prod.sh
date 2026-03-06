#!/usr/bin/env bash
set -euo pipefail

COMPOSE_FILE="docker-compose.yml"

echo "[prod.sh] Generating scaled compose file and nginx-conf from ${COMPOSE_FILE}"
./gen_compose.py ${COMPOSE_FILE}

echo "[prod.sh] podman-compose -f "docker-compose.scale.yml" $*"

podman-compose down 
podman-compose -f "docker-compose.scale.yml" "$@"
