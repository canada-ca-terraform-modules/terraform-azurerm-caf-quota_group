#!/bin/bash
# Submit a quota allocation request and poll until complete.
# Usage: allocate-quota.sh <url> <body_json>
set -euo pipefail

URL="$1"
BODY="$2"

echo "Submitting quota allocation request..."
az rest --method patch --url "$URL" --body "$BODY" 2>/dev/null

RESOURCE_NAME=$(echo "$BODY" | jq -r '.properties.value[0].properties.resourceName')
EXPECTED_LIMIT=$(echo "$BODY" | jq -r '.properties.value[0].properties.limit')

echo "Waiting for allocation to complete (Retry-After: 30s)..."
sleep 30

for i in $(seq 1 10); do
  CURRENT=$(az rest --method get --url "$URL" 2>/dev/null | jq -r ".value[] | select(.properties.resourceName==\"$RESOURCE_NAME\") | .properties.limit // -1")
  echo "  Poll $i: $RESOURCE_NAME limit=$CURRENT (expected=$EXPECTED_LIMIT)"
  if [ "$CURRENT" = "$EXPECTED_LIMIT" ]; then
    echo "  Allocation completed successfully."
    exit 0
  fi
  sleep 15
done

echo "  ERROR: Timed out waiting for allocation. Current limit=$CURRENT, expected=$EXPECTED_LIMIT"
exit 1
