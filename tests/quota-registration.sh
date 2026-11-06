#!/bin/bash
# Run: bash tests/quota-registration.sh
# Exercise the provisioner scripts without Azure credentials or real sleeps.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
TEST_DIR=$(mktemp -d)
trap 'rm -rf "$TEST_DIR"' EXIT
export TEST_DIR

az() {
  local method="" url="" body="" writes polls
  shift
  while (($#)); do
    case "$1" in
      --method) method="$2"; shift 2 ;;
      --url) url="$2"; shift 2 ;;
      --body) body="$2"; shift 2 ;;
      *) shift ;;
    esac
  done
  printf '%s %s %s\n' "$method" "$url" "$body" >> "$TEST_DIR/calls"
  if [[ "$url" == *"/providers/Microsoft.Quota?api-version=2021-04-01" ]]; then
    polls=$(cat "$TEST_DIR/polls")
    echo "$((polls + 1))" > "$TEST_DIR/polls"
    case "$SCENARIO" in
      registered) echo Registered ;;
      timeout) echo NotRegistered ;;
      poll_failure) echo "AuthorizationFailed checking registration" >&2; return 3 ;;
      *) if ((polls == 0)); then echo Registering; else echo Registered; fi ;;
    esac
    return 0
  fi
  if [[ "$method" == get ]]; then
    if [[ "$url" == *quotaAllocations* ]]; then
      echo '{"value":[{"properties":{"resourceName":"standarddasv5family","limit":50}}]}'
      return 0
    fi
    [[ "$SCENARIO" == existing ]]
    return $?
  fi
  writes=$(cat "$TEST_DIR/writes")
  echo "$((writes + 1))" > "$TEST_DIR/writes"
  if [[ "$SCENARIO" == success || "$SCENARIO" == retry && "$writes" -gt 0 ]]; then
    echo '{}'
    return 0
  fi
  case "$SCENARIO" in
    unrelated) echo 'ERROR: BadRequest: invalid quota' >&2 ;;
    forbidden) echo 'ERROR: Forbidden: Microsoft.Quota is registered' >&2 ;;
    *) echo 'ERROR: BadRequest: You do not have quota write permissions. Additionally, make sure that Microsoft.Quota is registered in this subscription.' >&2 ;;
  esac
  return 7
}

sleep() {
  echo "$1" >> "$TEST_DIR/sleeps"
}
export -f az sleep

MEMBERSHIP_URL="https://management.azure.com/providers/Microsoft.Management/managementGroups/mg/providers/Microsoft.Quota/groupQuotas/group/subscriptions/other-sub?api-version=2025-03-01"
ALLOCATION_URL="https://management.azure.com/providers/Microsoft.Management/managementGroups/mg/subscriptions/other-sub/providers/Microsoft.Quota/groupQuotas/group/resourceProviders/Microsoft.Compute/quotaAllocations/canadacentral?api-version=2025-03-01"
BODY='{"properties":{"value":[{"properties":{"resourceName":"standarddasv5family","limit":50}}]}}'

for script in add-subscription allocate-quota; do
  for scenario in existing success retry timeout registered unrelated forbidden poll_failure retry_failure; do
    [[ "$script" == allocate-quota && "$scenario" == existing ]] && continue
    export SCENARIO="$scenario"
    echo 0 > "$TEST_DIR/writes"
    echo 0 > "$TEST_DIR/polls"
    : > "$TEST_DIR/calls"
    : > "$TEST_DIR/sleeps"
    url="$MEMBERSHIP_URL"
    [[ "$script" == allocate-quota ]] && url="$ALLOCATION_URL"
    status=0
    bash "$ROOT/scripts/$script.sh" "$url" "$BODY" > "$TEST_DIR/output" 2>&1 || status=$?
    case "$scenario" in
      existing) [[ "$status" == 0 && $(cat "$TEST_DIR/writes") == 0 ]] ;;
      success) [[ "$status" == 0 && $(cat "$TEST_DIR/writes") == 1 && $(cat "$TEST_DIR/polls") == 0 ]] ;;
      retry)
        [[ "$status" == 0 && $(cat "$TEST_DIR/writes") == 2 && $(cat "$TEST_DIR/polls") == 2 ]]
        grep -q 'subscriptions/other-sub/providers/Microsoft.Quota?api-version=2021-04-01' "$TEST_DIR/calls"
        grep -q 'Registered; retrying' "$TEST_DIR/output"
        ;;
      timeout)
        [[ "$status" == 7 && $(cat "$TEST_DIR/writes") == 1 && $(cat "$TEST_DIR/polls") == 21 ]]
        [[ $(wc -l < "$TEST_DIR/sleeps") == 20 ]]
        grep -q 'Timed out after 300 seconds.*other-sub.*NotRegistered' "$TEST_DIR/output"
        ;;
      registered)
        [[ "$status" == 7 && $(cat "$TEST_DIR/writes") == 1 && $(cat "$TEST_DIR/polls") == 1 ]]
        grep -q 'check quota write permissions' "$TEST_DIR/output"
        ;;
      unrelated|forbidden) [[ "$status" == 7 && $(cat "$TEST_DIR/polls") == 0 ]] ;;
      poll_failure)
        [[ "$status" == 7 && $(cat "$TEST_DIR/writes") == 1 ]]
        grep -q 'Unable to check Microsoft.Quota registration' "$TEST_DIR/output"
        ;;
      retry_failure)
        [[ "$status" == 7 && $(cat "$TEST_DIR/writes") == 2 ]]
        grep -q 'You do not have quota write permissions' "$TEST_DIR/output"
        ;;
    esac
    if [[ "$script" == allocate-quota ]]; then
      grep -Fq "patch $ALLOCATION_URL $BODY" "$TEST_DIR/calls"
    fi
    echo "PASS: $script / $scenario"
  done
done
