#!/bin/bash
# Retry a quota write only when Microsoft.Quota registration is still pending.
az_rest_with_registration_retry() {
  local url="$1"
  shift
  local response status subscription_id provider_url state poll

  if response=$(az rest --url "$url" "$@" 2>&1); then
    printf '%s\n' "$response"
    return 0
  else
    status=$?
  fi

  if [[ "$response" != *BadRequest* || "$response" != *"Microsoft.Quota is registered"* ]]; then
    printf '%s\n' "$response" >&2
    return "$status"
  fi

  if [[ ! "$url" =~ /subscriptions/([^/?]+) ]]; then
    printf '%s\n' "$response" >&2
    return "$status"
  fi
  subscription_id="${BASH_REMATCH[1]}"
  provider_url="https://management.azure.com/subscriptions/$subscription_id/providers/Microsoft.Quota?api-version=2021-04-01"

  for ((poll = 0; poll <= 20; poll++)); do
    if ! state=$(az rest --method get --url "$provider_url" --query registrationState --output tsv); then
      echo "ERROR: Unable to check Microsoft.Quota registration on subscription $subscription_id." >&2
      printf '%s\n' "$response" >&2
      return "$status"
    fi

    if [[ "$state" == "Registered" ]]; then
      if ((poll == 0)); then
        echo "Microsoft.Quota is already Registered on subscription $subscription_id; check quota write permissions." >&2
        printf '%s\n' "$response" >&2
        return "$status"
      fi
      echo "Microsoft.Quota is Registered; retrying quota request." >&2
      az rest --url "$url" "$@"
      return $?
    fi

    if ((poll < 20)); then
      echo "Waiting for Microsoft.Quota registration on subscription $subscription_id (state=$state)..." >&2
      sleep 15
    fi
  done

  echo "ERROR: Timed out after 300 seconds waiting for Microsoft.Quota registration on subscription $subscription_id (state=$state). Register Microsoft.Quota and order the module after registration with depends_on." >&2
  printf '%s\n' "$response" >&2
  return "$status"
}
