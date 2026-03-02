#!/usr/bin/env bash
set -euo pipefail

i="$1"

curl -sS -N -G "http://localhost:8502/api/chatbot/streamresponse" \
  --data-urlencode "input=plot x=y" \
  -H "Authorization: Bearer <YOUR_TOKEN>" \
  -H "x-freva-rest-url: http://rest.example" \
  -H "x-freva-vault-url: mongodb://<YOUR_MONGO_URI_OR_VAULT_URL>" \
  -H "x-freva-config-path: /tmp/config.yml" \
  > "out_$i.ndjson"


# seq 1 20 | xargs -n1 -P20 ./one_call.sh