"""Quota transfer logic: subscription -> quota group."""

import asyncio
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import AsyncGenerator

from .az_rest import AzRestError, az_rest

logger = logging.getLogger(__name__)

API_VERSION = "2025-03-01"


class TransferStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class JobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"


@dataclass
class SkuTransferResult:
    resource_name: str
    display_name: str
    original_limit: int
    new_limit: int
    status: TransferStatus = TransferStatus.PENDING
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "resourceName": self.resource_name,
            "displayName": self.display_name,
            "originalLimit": self.original_limit,
            "newLimit": self.new_limit,
            "status": self.status.value,
            "error": self.error,
        }


@dataclass
class TransferJob:
    """Tracks the state of a batch transfer."""

    job_id: str
    management_group_id: str
    quota_group_name: str
    subscription_id: str
    subscription_name: str
    location: str
    leave_amount: int
    skus: list[SkuTransferResult] = field(default_factory=list)
    state: JobState = JobState.QUEUED
    # Subscribers waiting for updates (SSE connections)
    _update_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    @property
    def allocation_url(self) -> str:
        return (
            f"https://management.azure.com/providers/Microsoft.Management"
            f"/managementGroups/{self.management_group_id}"
            f"/subscriptions/{self.subscription_id}"
            f"/providers/Microsoft.Quota/groupQuotas/{self.quota_group_name}"
            f"/resourceProviders/Microsoft.Compute"
            f"/quotaAllocations/{self.location}"
            f"?api-version={API_VERSION}"
        )

    @property
    def compute_usages_url(self) -> str:
        """The subscription's actual compute usages endpoint — shows real current limits."""
        return (
            f"https://management.azure.com/subscriptions/{self.subscription_id}"
            f"/providers/Microsoft.Compute/locations/{self.location}"
            f"/usages?api-version=2023-07-01"
        )

    def notify_update(self):
        """Signal that something changed so SSE listeners can push updates."""
        self._update_event.set()
        self._update_event = asyncio.Event()

    async def wait_for_update(self, timeout: float = 2.0) -> bool:
        """Wait for the next state change. Returns True if woken by an update."""
        try:
            await asyncio.wait_for(self._update_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    def summary(self) -> dict:
        """Compact summary for the job list API."""
        succeeded = sum(1 for s in self.skus if s.status == TransferStatus.SUCCEEDED)
        failed = sum(1 for s in self.skus if s.status == TransferStatus.FAILED)
        in_progress = sum(1 for s in self.skus if s.status == TransferStatus.IN_PROGRESS)
        pending = sum(1 for s in self.skus if s.status == TransferStatus.PENDING)
        return {
            "jobId": self.job_id,
            "subscriptionId": self.subscription_id,
            "subscriptionName": self.subscription_name,
            "location": self.location,
            "quotaGroupName": self.quota_group_name,
            "state": self.state.value,
            "total": len(self.skus),
            "succeeded": succeeded,
            "failed": failed,
            "inProgress": in_progress,
            "pending": pending,
            "skus": [s.to_dict() for s in self.skus],
        }


async def execute_transfer(job: TransferJob) -> None:
    """Execute all SKU transfers sequentially in the background.

    Updates job.skus in place and signals listeners via notify_update().
    """
    job.state = JobState.RUNNING
    job.notify_update()

    for sku in job.skus:
        sku.status = TransferStatus.IN_PROGRESS
        job.notify_update()

        try:
            body = {
                "properties": {
                    "value": [
                        {
                            "properties": {
                                "limit": sku.new_limit,
                                "resourceName": sku.resource_name,
                            }
                        }
                    ]
                }
            }
            await az_rest("patch", job.allocation_url, body)

            # Poll for completion using subscription compute usages
            success = await _poll_allocation_sequential(
                job.compute_usages_url, sku.resource_name, sku.new_limit, sku
            )
            if success:
                sku.status = TransferStatus.SUCCEEDED
            else:
                sku.status = TransferStatus.FAILED
                sku.error = "Timed out waiting for allocation to complete"
        except AzRestError as e:
            sku.status = TransferStatus.FAILED
            sku.error = str(e)
        except Exception as e:
            sku.status = TransferStatus.FAILED
            sku.error = f"Unexpected error: {e}"

        job.notify_update()

    job.state = JobState.COMPLETED
    job.notify_update()


async def _poll_allocation_sequential(
    url: str, resource_name: str, expected_limit: int,
    sku: SkuTransferResult,
    max_polls: int = 20, interval: int = 15
) -> bool:
    """Poll the compute usages endpoint until the subscription limit matches expected."""
    # Initial wait before first poll
    await asyncio.sleep(20)

    for i in range(max_polls):
        try:
            response = await az_rest("get", url)
            values = response.get("value", [])
            current = None
            found = False
            for v in values:
                name_obj = v.get("name", {})
                if name_obj.get("value") == resource_name:
                    current = v.get("limit")
                    found = True
                    logger.info(
                        "Poll %d: %s limit=%s (expected=%s)",
                        i + 1,
                        resource_name,
                        current,
                        expected_limit,
                    )
                    if int(current) == int(expected_limit):
                        return True
                    break

            # If resource not found and we expect 0, that's success
            if not found and expected_limit == 0:
                logger.info("Poll %d: %s not found — expected 0, success", i + 1, resource_name)
                return True

        except AzRestError as e:
            logger.warning("Poll %d failed for %s: %s", i + 1, resource_name, e)

        await asyncio.sleep(interval)

    return False
