#!/bin/bash
# Return a single SKU's quota allocation back to the group (set limit to 10).
# Usage: deallocate-quota.sh <url> <resource_name>
set -euo pipefail

URL="$1"
RESOURCE_NAME="$2"
MIN_LIMIT=10

# Check current limit — skip if already at minimum
CURRENT=$(az rest --method get --url "$URL" 2>/dev/null | jq -r ".value[] | select(.properties.resourceName==\"$RESOURCE_NAME\") | .properties.limit // -1")

if [ "$CURRENT" = "$MIN_LIMIT" ] || [ "$CURRENT" = "-1" ]; then
  echo "  $RESOURCE_NAME already at minimum ($MIN_LIMIT) or not found. Nothing to return."
  exit 0
fi

echo "  Returning quota for $RESOURCE_NAME: $CURRENT -> $MIN_LIMIT"

BODY=$(jq -n --arg rn "$RESOURCE_NAME" --argjson limit "$MIN_LIMIT" '{
  properties: {
    value: [{
      properties: {
        limit: $limit,
        resourceName: $rn
      }
    }]
  }
}')

az rest --method patch --url "$URL" --body "$BODY" 2>/dev/null || {
  echo "  WARN: PATCH request failed for $RESOURCE_NAME"
  exit 1
}

echo "  Waiting for deallocation (Retry-After: 30s)..."
sleep 30

for i in $(seq 1 10); do
  NEW_LIMIT=$(az rest --method get --url "$URL" 2>/dev/null | jq -r ".value[] | select(.properties.resourceName==\"$RESOURCE_NAME\") | .properties.limit // -1")
  echo "    Poll $i: $RESOURCE_NAME limit=$NEW_LIMIT (expected=$MIN_LIMIT)"
  if [ "$NEW_LIMIT" = "$MIN_LIMIT" ]; then
    echo "  Deallocation confirmed for $RESOURCE_NAME."
    exit 0
  fi
  sleep 15
done

echo "  ERROR: Timed out waiting for deallocation of $RESOURCE_NAME. Current=$NEW_LIMIT, expected=$MIN_LIMIT"
exit 1
