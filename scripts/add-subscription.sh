#!/bin/bash
# Add a subscription to a quota group (idempotent).
# Usage: add-subscription.sh <url>
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/quota-registration.sh"

URL="$1"

if az rest --method get --url "$URL" 2>/dev/null; then
  echo "Subscription already in quota group, skipping."
else
  az_rest_with_registration_retry "$URL" --method put
fi
