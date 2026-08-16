#!/bin/bash
# Add a subscription to a quota group (idempotent).
# Usage: add-subscription.sh <url>
set -euo pipefail

URL="$1"

az rest --method get --url "$URL" 2>/dev/null \
  && echo "Subscription already in quota group, skipping." \
  || az rest --method put --url "$URL"
