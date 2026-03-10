#!/usr/bin/env bash
set -euo pipefail

PARALLEL="${1:-20}"
LABEL="${2:-run}"
BASE_DIR="${3:-performance_exp}"

RUN_DIR="${BASE_DIR}/${LABEL}"

rm -rf "$RUN_DIR"
mkdir -p "$RUN_DIR"

start_ns=$(date +%s%N)

seq 1 "$PARALLEL" | xargs -I{} -P"$PARALLEL" ./scripts/performance_exp/one_call.sh {} "$RUN_DIR"

end_ns=$(date +%s%N)
batch_sec=$(awk "BEGIN { printf \"%.3f\", ($end_ns - $start_ns)/1000000000 }")

cat "$RUN_DIR"/metrics_*.csv > "$RUN_DIR/all_metrics.csv"

cat > "$RUN_DIR/run_info.env" <<EOF
label=$LABEL
parallel=$PARALLEL
requests=$PARALLEL
batch_seconds=$batch_sec
EOF

echo "label=$LABEL parallel=$PARALLEL requests=$PARALLEL batch_seconds=$batch_sec"
