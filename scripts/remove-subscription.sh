#!/bin/bash
# Remove a subscription from a quota group.
# Safety net: attempts to return any remaining quota before removal.
# Primary deallocation is handled by each allocation resource's own destroy provisioner.
#
# Usage: remove-subscription.sh <cleanup_commands_json> <remove_url>
set -euo pipefail

CMDS="$1"
REMOVE_URL="$2"

echo "Checking for any remaining quota to return before removing subscription..."

if [ "$CMDS" != "[]" ] && [ -n "$CMDS" ]; then
  for cmd in $(echo "$CMDS" | jq -c '.[]'); do
    url=$(echo "$cmd" | jq -r '.url')
    body=$(echo "$cmd" | jq -r '.body')
    resource_name=$(echo "$body" | jq -r '.properties.value[0].properties.resourceName')
    expected_limit=$(echo "$body" | jq -r '.properties.value[0].properties.limit')

    # Check if already at minimum (likely already handled by allocation destroy)
    current_sub_limit=$(az rest --method get --url "$url" 2>/dev/null | jq -r ".value[] | select(.properties.resourceName==\"$resource_name\") | .properties.limit // -1")

    if [ "$current_sub_limit" = "$expected_limit" ] || [ "$current_sub_limit" = "-1" ]; then
      echo "  $resource_name: already at minimum or not found. OK."
      continue
    fi

    # Still has quota — return it (allocation destroy may not have run yet)
    echo "  $resource_name: still at $current_sub_limit, returning to $expected_limit..."
    az rest --method patch --url "$url" --body "$body" 2>/dev/null || {
      echo "  WARN: Failed to return quota for $resource_name. Proceeding anyway."
      continue
    }

    sleep 30
    for i in $(seq 1 10); do
      new_limit=$(az rest --method get --url "$url" 2>/dev/null | jq -r ".value[] | select(.properties.resourceName==\"$resource_name\") | .properties.limit // -1")
      echo "    Poll $i: $resource_name limit=$new_limit (expected=$expected_limit)"
      if [ "$new_limit" = "$expected_limit" ]; then
        echo "  $resource_name: returned successfully."
        break
      fi
      sleep 15
    done

    if [ "$new_limit" != "$expected_limit" ]; then
      echo "  ERROR: Timed out returning quota for $resource_name. Aborting to prevent quota loss."
      exit 1
    fi
  done
fi

echo "Removing subscription from quota group..."
az rest --method delete --url "$REMOVE_URL"
echo "Subscription removed."
