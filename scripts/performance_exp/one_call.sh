#!/usr/bin/env bash
set -euo pipefail

i="$1"

RUN_DIR="${2:-scripts/performance_exp/run}"

THREAD_ID="t-$(uuidgen)"

curl -sS -N -G "http://localhost:8502/api/chatbot/streamresponse" \
  --data-urlencode "thread_id=$THREAD_ID" \
  --data-urlencode "input=plot x=y" \
  -H "Authorization: Bearer <YOUR_TOKEN>" \
  -H "x-freva-rest-url: http://rest.example" \
  -H "x-freva-vault-url: mongodb://<YOUR_MONGO_URI_OR_VAULT_URL>" \
  -H "x-freva-config-path: /tmp/config.yml" \
  -o "$RUN_DIR/out_$i.ndjson" \
  -w "$i,%{http_code},%{time_connect},%{time_starttransfer},%{time_total},%{size_download}\n" \
  > "$RUN_DIR/metrics_$i.csv"

# seq 1 20 | xargs -n1 -P20 ./one_call.sh