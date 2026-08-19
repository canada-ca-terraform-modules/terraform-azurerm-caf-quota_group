"""Wrapper around `az rest` CLI calls with concurrency control."""

import asyncio
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Semaphore to limit concurrent az rest calls (avoid API throttling)
_semaphore = asyncio.Semaphore(3)

# Max retries for throttled requests
MAX_RETRIES = 3


async def az_rest(method: str, url: str, body: dict | None = None) -> dict[str, Any]:
    """Execute an az rest call and return parsed JSON response. Retries on throttling."""
    for attempt in range(MAX_RETRIES + 1):
        async with _semaphore:
            cmd = ["az", "rest", "--method", method, "--url", url]
            if body is not None:
                cmd.extend(["--body", json.dumps(body)])

            logger.debug("az rest %s %s (attempt %d)", method, url, attempt + 1)
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()

            if proc.returncode != 0:
                err_msg = stderr.decode().strip()

                # Check for throttling
                if "RequestThrottled" in err_msg or "Too Many Requests" in err_msg or "429" in err_msg:
                    if attempt < MAX_RETRIES:
                        # Parse retry-after or use exponential backoff
                        wait_time = 60 * (attempt + 1)  # 60s, 120s, 180s
                        logger.warning(
                            "Throttled on %s %s. Retrying in %ds (attempt %d/%d)",
                            method, url, wait_time, attempt + 1, MAX_RETRIES,
                        )
                        await asyncio.sleep(wait_time)
                        continue

                raise AzRestError(
                    f"az rest {method} failed (rc={proc.returncode}): {err_msg}",
                    status_code=proc.returncode,
                    stderr=err_msg,
                )

            if not stdout.strip():
                return {}
            return json.loads(stdout)

    # Should not reach here, but just in case
    raise AzRestError("Max retries exceeded")


class AzRestError(Exception):
    """Error from an az rest call."""

    def __init__(self, message: str, status_code: int = 1, stderr: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.stderr = stderr
