#!/usr/bin/env bash
set -euo pipefail

SERVICE_REPLICAS="${1:?usage: ./run_parallel_sweep.sh <service_replicas> [parallel_list]}"
PARALLEL_LIST="${2:-1 2 4 8 12 16 20}"

BASE_DIR="performance_exp/results/r${SERVICE_REPLICAS}"
mkdir -p "$BASE_DIR"

echo "Running parallel sweep for deployment with service replicas = $SERVICE_REPLICAS"
echo "Parallel levels: $PARALLEL_LIST"
echo

for p in $PARALLEL_LIST; do
  label="r${SERVICE_REPLICAS}_p${p}"
  echo "==> $label"
  ./performance_exp/bench.sh "$p" "$label" "$BASE_DIR"
done

echo
echo "Done. Results in $BASE_DIR"
echo "Now running:"
echo "./performance_exp/summarize_plot.py \"$BASE_DIR\""

./performance_exp/summarize_plot.sh \"$BASE_DIR\"