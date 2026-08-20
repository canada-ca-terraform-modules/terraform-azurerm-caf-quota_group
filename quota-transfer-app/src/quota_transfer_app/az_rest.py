"""Azure REST client with adaptive rate limiting.

Uses httpx for direct HTTP calls (instead of az rest CLI) to access response headers.
Implements AIMD (additive increase, multiplicative decrease) rate control based on
x-ms-ratelimit-remaining-* headers from Azure responses.
"""

import asyncio
import json
import logging
import subprocess
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# ─── Token Management ─────────────────────────────────────────────────────────

_token_cache: dict[str, Any] = {"access_token": None, "expires_on": 0}
_token_lock = asyncio.Lock()


async def _get_access_token() -> str:
    """Get a valid Azure access token, refreshing if expired."""
    async with _token_lock:
        now = time.time()
        # Refresh 5 minutes before expiry
        if _token_cache["access_token"] and _token_cache["expires_on"] > now + 300:
            return _token_cache["access_token"]

        logger.debug("Refreshing Azure access token")
        proc = await asyncio.create_subprocess_exec(
            "az", "account", "get-access-token",
            "--resource", "https://management.azure.com",
            "--output", "json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            raise AzRestError(
                f"Failed to get access token: {stderr.decode().strip()}",
                status_code=proc.returncode,
            )

        data = json.loads(stdout)
        _token_cache["access_token"] = data["accessToken"]
        # Parse expiresOn — it's a datetime string like "2026-08-19 12:05:17.000000"
        from datetime import datetime
        try:
            expires_dt = datetime.strptime(data["expiresOn"], "%Y-%m-%d %H:%M:%S.%f")
            _token_cache["expires_on"] = expires_dt.timestamp()
        except (ValueError, KeyError):
            # Fallback: assume 1 hour validity
            _token_cache["expires_on"] = now + 3600

        return _token_cache["access_token"]


# ─── Adaptive Rate Limiter (per endpoint bucket) ──────────────────────────────


@dataclass
class RateBucket:
    """Adaptive rate limiter per endpoint bucket.

    Strategy:
    1. If we know the rate limit window (from headers), compute optimal interval directly
    2. Use remaining/limit ratio to proportionally adjust speed
    3. Detect declining remaining trend and preemptively slow
    4. On 429: use Retry-After header as truth, then resume at calculated safe rate
    5. Recovery is fast when headers show headroom, slow when blind
    """

    name: str
    # Minimum interval between requests (seconds) — dynamically adjusted
    min_interval: float = 2.0
    # Timestamp of last successful request
    last_request_at: float = 0.0
    # History of (timestamp, remaining, limit) for trend/window detection
    remaining_history: deque = field(default_factory=lambda: deque(maxlen=30))
    # Number of consecutive successes without throttling
    success_streak: int = 0
    # Total throttle hits (for stats)
    throttle_count: int = 0
    # Inferred window size (seconds) — derived from observing remaining refills
    _inferred_window: float | None = field(default=None, repr=False)
    # Last known limit value
    _last_known_limit: int | None = field(default=None, repr=False)

    # Hard boundaries
    MIN_INTERVAL_FLOOR: float = 0.5   # Default — overridden per bucket at creation
    MIN_INTERVAL_CEILING: float = 120.0  # Never slower than 1 req/2min
    # Safety margin: use only this fraction of the theoretical max rate
    SAFETY_MARGIN: float = 0.80

    def _compute_optimal_interval(self, remaining: int, limit: int) -> float:
        """Compute the optimal interval given current remaining quota and limit.

        If we know the window, calculate: time_left_in_window / remaining_requests
        If not, use the ratio to scale between floor and a conservative max.
        """
        if limit <= 0:
            return self.min_interval

        fraction_remaining = remaining / limit

        # If we have an inferred window, do the direct calculation
        if self._inferred_window and remaining > 0:
            # Estimate time remaining in the current window
            # Use the consumption rate to infer where we are in the window
            if len(self.remaining_history) >= 2:
                oldest_ts, oldest_rem, _ = self.remaining_history[0]
                newest_ts, newest_rem, _ = self.remaining_history[-1]
                time_span = newest_ts - oldest_ts
                consumed = oldest_rem - newest_rem

                if consumed > 0 and time_span > 0:
                    # Rate of consumption
                    consumption_rate = consumed / time_span  # requests per second consumed
                    # At this rate, how long until we exhaust remaining?
                    time_to_exhaustion = remaining / consumption_rate
                    # Set interval so we spread remaining requests over available time
                    optimal = (time_to_exhaustion / remaining) * self.SAFETY_MARGIN
                    return max(optimal, self.MIN_INTERVAL_FLOOR)

        # Fallback: scale interval based on fraction remaining
        # At 100% remaining → go at floor speed
        # At 50% → 2x floor
        # At 25% → 4x floor
        # At 10% → 10x floor
        if fraction_remaining > 0.5:
            # Plenty of headroom — go fast
            return self.MIN_INTERVAL_FLOOR
        elif fraction_remaining > 0.25:
            # Moderate — scale linearly
            scale = 1.0 + (2.0 * (1.0 - fraction_remaining * 2))
            return self.MIN_INTERVAL_FLOOR * scale
        elif fraction_remaining > 0.1:
            # Getting low — be cautious
            scale = 4.0 + (6.0 * (0.25 - fraction_remaining) / 0.15)
            return self.MIN_INTERVAL_FLOOR * scale
        else:
            # Critical — go very slow
            return min(30.0, self.MIN_INTERVAL_CEILING)

    def _detect_declining_trend(self) -> bool:
        """Check if remaining is declining across recent calls."""
        if len(self.remaining_history) < 4:
            return False
        # Look at last 4 entries
        recent = [r for _, r, _ in list(self.remaining_history)[-4:]]
        # Declining if each is less than or equal to the previous
        declining = all(recent[i] <= recent[i - 1] for i in range(1, len(recent)))
        # And total drop is significant (more than 20% of first value)
        if recent[0] > 0:
            drop_fraction = (recent[0] - recent[-1]) / recent[0]
            return declining and drop_fraction > 0.2
        return declining

    def record_success(self, remaining: int | None, limit: int | None):
        """Record a successful call and intelligently adjust rate."""
        now = time.time()
        self.last_request_at = now
        self.success_streak += 1

        if limit is not None:
            self._last_known_limit = limit

        if remaining is not None:
            effective_limit = limit or self._last_known_limit or 100
            self.remaining_history.append((now, remaining, effective_limit))

            # Try to detect window resets (remaining suddenly jumps up)
            if len(self.remaining_history) >= 2:
                prev_ts, prev_rem, _ = self.remaining_history[-2]
                if remaining > prev_rem + 5:  # Jumped up significantly = window reset
                    window_duration = now - prev_ts
                    if window_duration > 10:  # Sanity check
                        self._inferred_window = window_duration
                        logger.info(
                            "Bucket %s: detected rate limit window reset (~%.0fs window, limit=%d)",
                            self.name, window_duration, effective_limit,
                        )
                        _schedule_rate_save()

            # Compute optimal interval based on current state
            optimal = self._compute_optimal_interval(remaining, effective_limit)

            # If trend is declining, be more conservative
            if self._detect_declining_trend():
                optimal = max(optimal, self.min_interval)  # Don't speed up during decline
                # Actually slow down a bit
                optimal *= 1.3
                logger.debug("Bucket %s: declining trend, conservative interval %.1fs", self.name, optimal)

            # Apply: move towards optimal, but smoothly
            if optimal < self.min_interval:
                # Speed up: move quickly towards optimal (50% of the gap per success)
                self.min_interval -= (self.min_interval - optimal) * 0.5
            else:
                # Slow down: move immediately to optimal (safety first)
                self.min_interval = optimal

            self.min_interval = max(self.min_interval, self.MIN_INTERVAL_FLOOR)
            self.min_interval = min(self.min_interval, self.MIN_INTERVAL_CEILING)

        else:
            # No headers — blind mode, cautious additive increase after streak
            if self.success_streak >= 3:
                self.min_interval = max(
                    self.min_interval * 0.9,  # 10% faster per 3 successes
                    self.MIN_INTERVAL_FLOOR,
                )

    def record_throttle(self, retry_after: float | None):
        """Record a throttle (429) and derive safe rate from the signal."""
        self.throttle_count += 1
        self.success_streak = 0

        old_interval = self.min_interval

        if retry_after:
            # Retry-After is the strongest signal — it tells us exactly how long to wait
            # After that wait, we know the window has (partially) reset
            # Set ongoing interval based on: we were going too fast by at least 2x
            self.min_interval = max(retry_after, self.min_interval * 2.0)
        else:
            # No Retry-After: double the interval (we were going 2x too fast)
            self.min_interval *= 2.0

        self.min_interval = min(self.min_interval, self.MIN_INTERVAL_CEILING)

        logger.warning(
            "Bucket %s: 429 received! interval %.1f→%.1fs (throttle #%d)",
            self.name, old_interval, self.min_interval, self.throttle_count,
        )

        # Clear remaining history — it's stale after throttle
        self.remaining_history.clear()

        # Auto-save after throttle — this is a significant learning event
        _schedule_rate_save()

    async def wait_for_slot(self):
        """Wait until enough time has passed since the last request."""
        now = time.time()
        elapsed = now - self.last_request_at
        if elapsed < self.min_interval:
            wait = self.min_interval - elapsed
            logger.debug("Bucket %s: rate-limiting, waiting %.1fs", self.name, wait)
            await asyncio.sleep(wait)

    def stats(self) -> dict:
        """Return current stats for this bucket."""
        last_remaining = None
        last_limit = None
        if self.remaining_history:
            _, last_remaining, last_limit = self.remaining_history[-1]
        return {
            "name": self.name,
            "minInterval": round(self.min_interval, 1),
            "effectiveRpm": round(60.0 / self.min_interval, 1) if self.min_interval > 0 else 999,
            "successStreak": self.success_streak,
            "throttleCount": self.throttle_count,
            "lastRemaining": last_remaining,
            "lastLimit": last_limit,
            "inferredWindow": round(self._inferred_window, 0) if self._inferred_window else None,
        }


# Global rate buckets
_rate_buckets: dict[str, RateBucket] = {}
_bucket_lock = asyncio.Lock()


def _endpoint_key(url: str) -> str:
    """Extract a rate-limit bucket key from a URL."""
    if "quotaAllocations" in url:
        return "quotaAllocations"
    if "groupQuotaLimits" in url:
        return "groupQuotaLimits"
    if "/usages" in url:
        return "computeUsages"
    if "groupQuotas" in url and "/subscriptions/" in url:
        return "groupQuotaSubscriptions"
    if "managementGroups" in url:
        return "managementGroups"
    if "/subscriptions" in url and "providers" not in url:
        return "subscriptions"
    return "default"


async def _get_bucket(key: str) -> RateBucket:
    """Get or create a rate bucket for an endpoint."""
    async with _bucket_lock:
        if key not in _rate_buckets:
            # Different starting intervals based on endpoint sensitivity
            initial_intervals = {
                "quotaAllocations": 90.0,  # Writes to quota — observed: ~5 req/10min window
                "groupQuotaLimits": 3.0,   # Reads — moderately limited
                "computeUsages": 2.0,      # Standard compute reads
                "groupQuotaSubscriptions": 5.0,  # Subscription membership writes
                "managementGroups": 2.0,
                "subscriptions": 1.0,
                "default": 2.0,
            }
            # Per-bucket floors — how fast this bucket can EVER go
            floor_intervals = {
                "quotaAllocations": 90.0,  # Never faster than ~0.67 req/min (API allows ~5 req/10min)
                "groupQuotaSubscriptions": 5.0,
                "groupQuotaLimits": 1.0,
                "computeUsages": 0.5,
                "managementGroups": 0.5,
                "subscriptions": 0.5,
                "default": 0.5,
            }
            _rate_buckets[key] = RateBucket(
                name=key,
                min_interval=initial_intervals.get(key, 2.0),
                MIN_INTERVAL_FLOOR=floor_intervals.get(key, 0.5),
            )
        return _rate_buckets[key]


def _parse_rate_limit_headers(headers: httpx.Headers) -> tuple[int | None, int | None]:
    """Extract remaining and limit from Azure rate-limit response headers.

    Azure uses various header names:
    - x-ms-ratelimit-remaining-subscription-reads
    - x-ms-ratelimit-remaining-subscription-writes
    - x-ms-ratelimit-remaining-tenant-reads
    - x-ms-ratelimit-remaining-tenant-writes
    - x-ms-ratelimit-remaining-subscription-resource-requests
    """
    remaining = None
    limit = None

    for key, value in headers.items():
        key_lower = key.lower()
        if "ratelimit-remaining" in key_lower:
            try:
                remaining = int(value)
            except (ValueError, TypeError):
                pass
        elif key_lower == "x-ms-ratelimit-limit":
            try:
                limit = int(value)
            except (ValueError, TypeError):
                pass

    return remaining, limit


# ─── Write semaphore (only one write at a time globally) ──────────────────────
_write_semaphore = asyncio.Semaphore(1)

# Max retries
MAX_RETRIES = 5

# Throttle state for UI visibility
throttle_state: dict[str, Any] = {
    "is_throttled": False,
    "retry_after": 0,
    "last_throttled_at": None,
}

# Shared httpx client
_http_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    """Get or create the shared httpx client."""
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=30.0))
    return _http_client


async def az_rest(method: str, url: str, body: dict | None = None) -> dict[str, Any]:
    """Execute an Azure REST call with adaptive rate limiting.

    Uses httpx directly to access response headers for rate-limit tracking.
    Implements AIMD rate control to maximize throughput without hitting 429s.
    """
    endpoint_key = _endpoint_key(url)
    bucket = await _get_bucket(endpoint_key)
    is_write = method.lower() in ("put", "patch", "post", "delete")

    for attempt in range(MAX_RETRIES + 1):
        # Adaptive rate limiting: wait for our slot
        await bucket.wait_for_slot()

        # For writes, also acquire the global write semaphore
        if is_write:
            await _write_semaphore.acquire()

        try:
            token = await _get_access_token()
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }

            client = _get_client()
            logger.debug("httpx %s %s (attempt %d)", method.upper(), url, attempt + 1)

            if body is not None:
                response = await client.request(method.upper(), url, headers=headers, json=body)
            else:
                response = await client.request(method.upper(), url, headers=headers)

            # Parse rate-limit headers regardless of status
            remaining, limit = _parse_rate_limit_headers(response.headers)

            if response.status_code == 429:
                # Throttled
                retry_after_header = response.headers.get("retry-after")
                retry_after = None
                if retry_after_header:
                    try:
                        retry_after = int(retry_after_header)
                    except ValueError:
                        retry_after = None

                bucket.record_throttle(retry_after)

                if attempt < MAX_RETRIES:
                    wait_time = retry_after or bucket.min_interval
                    throttle_state["is_throttled"] = True
                    throttle_state["retry_after"] = wait_time
                    throttle_state["last_throttled_at"] = time.time()

                    logger.warning(
                        "429 on %s %s. Waiting %.0fs (attempt %d/%d, bucket interval now %.1fs)",
                        method.upper(), url, wait_time, attempt + 1, MAX_RETRIES,
                        bucket.min_interval,
                    )
                    await asyncio.sleep(wait_time)
                    throttle_state["is_throttled"] = False
                    continue

                # Out of retries
                raise AzRestError(
                    f"429 Too Many Requests after {MAX_RETRIES} retries: {response.text}",
                    status_code=429,
                    stderr=response.text,
                )

            elif response.status_code >= 400:
                raise AzRestError(
                    f"HTTP {response.status_code}: {response.text}",
                    status_code=response.status_code,
                    stderr=response.text,
                )

            # Success — record for rate adaptation
            bucket.record_success(remaining, limit)

            if not response.text.strip():
                return {}
            return response.json()

        finally:
            if is_write:
                _write_semaphore.release()

    raise AzRestError(f"Max retries ({MAX_RETRIES}) exceeded for {method} {url}")


def get_rate_stats() -> dict[str, Any]:
    """Get current rate-limiting stats for all buckets (for UI display)."""
    return {
        "buckets": {k: v.stats() for k, v in _rate_buckets.items()},
        "throttle": dict(throttle_state),
    }


RATE_SAVE_PATH = "/tmp/quota-transfer-rates.json"

# Debounced auto-save: saves at most once every 30 seconds
_rate_save_scheduled: bool = False
_last_rate_save: float = 0.0
_RATE_SAVE_INTERVAL: float = 30.0  # seconds between auto-saves


def _schedule_rate_save():
    """Schedule an auto-save of rate state (debounced)."""
    global _rate_save_scheduled
    if _rate_save_scheduled:
        return
    _rate_save_scheduled = True

    async def _do_save():
        global _rate_save_scheduled, _last_rate_save
        now = time.time()
        elapsed = now - _last_rate_save
        if elapsed < _RATE_SAVE_INTERVAL:
            await asyncio.sleep(_RATE_SAVE_INTERVAL - elapsed)
        save_rate_state()
        _last_rate_save = time.time()
        _rate_save_scheduled = False

    try:
        asyncio.get_event_loop().create_task(_do_save())
    except RuntimeError:
        # No event loop — skip (happens during testing)
        _rate_save_scheduled = False


def save_rate_state() -> None:
    """Persist learned rate limits to disk."""
    import json as _json
    from pathlib import Path

    data = {}
    for key, bucket in _rate_buckets.items():
        data[key] = {
            "min_interval": bucket.min_interval,
            "throttle_count": bucket.throttle_count,
            "inferred_window": bucket._inferred_window,
            "last_known_limit": bucket._last_known_limit,
        }

    Path(RATE_SAVE_PATH).write_text(_json.dumps(data, indent=2))
    logger.info("Saved rate state for %d buckets to %s", len(data), RATE_SAVE_PATH)


def load_rate_state() -> None:
    """Load previously learned rate limits from disk."""
    import json as _json
    from pathlib import Path

    path = Path(RATE_SAVE_PATH)
    if not path.exists():
        logger.info("No saved rate state found at %s", RATE_SAVE_PATH)
        return

    try:
        data = _json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to load rate state: %s", e)
        return

    for key, state in data.items():
        if key not in _rate_buckets:
            initial_intervals = {
                "quotaAllocations": 90.0,
                "groupQuotaLimits": 3.0,
                "computeUsages": 2.0,
                "groupQuotaSubscriptions": 5.0,
                "managementGroups": 2.0,
                "subscriptions": 1.0,
                "default": 2.0,
            }
            floor_intervals = {
                "quotaAllocations": 90.0,
                "groupQuotaSubscriptions": 5.0,
                "groupQuotaLimits": 1.0,
                "computeUsages": 0.5,
                "managementGroups": 0.5,
                "subscriptions": 0.5,
                "default": 0.5,
            }
            _rate_buckets[key] = RateBucket(
                name=key,
                min_interval=initial_intervals.get(key, 2.0),
                MIN_INTERVAL_FLOOR=floor_intervals.get(key, 0.5),
            )

        bucket = _rate_buckets[key]
        loaded_interval = state.get("min_interval", bucket.min_interval)
        # Enforce floor — never load a value below the bucket's floor
        bucket.min_interval = max(loaded_interval, bucket.MIN_INTERVAL_FLOOR)
        bucket.throttle_count = state.get("throttle_count", 0)
        bucket._inferred_window = state.get("inferred_window")
        bucket._last_known_limit = state.get("last_known_limit")

    logger.info(
        "Loaded rate state for %d buckets from %s: %s",
        len(data),
        RATE_SAVE_PATH,
        {k: f"{v.min_interval:.1f}s ({60/v.min_interval:.1f} rpm)" for k, v in _rate_buckets.items()},
    )


class AzRestError(Exception):
    """Error from an Azure REST call."""

    def __init__(self, message: str, status_code: int = 1, stderr: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.stderr = stderr
