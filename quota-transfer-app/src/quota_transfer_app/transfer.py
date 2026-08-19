"""Quota transfer logic: subscription -> quota group."""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncGenerator

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
    PAUSED = "paused"
    COMPLETED = "completed"


@dataclass
class SkuTransferResult:
    resource_name: str
    display_name: str
    original_limit: int
    new_limit: int
    status: TransferStatus = TransferStatus.PENDING
    error: str = ""
    started_at: float | None = None   # time.time() when IN_PROGRESS
    completed_at: float | None = None  # time.time() when SUCCEEDED/FAILED

    def to_dict(self) -> dict:
        return {
            "resourceName": self.resource_name,
            "displayName": self.display_name,
            "originalLimit": self.original_limit,
            "newLimit": self.new_limit,
            "status": self.status.value,
            "error": self.error,
            "startedAt": self.started_at,
            "completedAt": self.completed_at,
        }


@dataclass
class PreStep:
    """A prerequisite step (provider registration, group membership) tracked in the job."""

    name: str        # e.g. "register_provider", "add_to_group"
    display_name: str  # e.g. "Register Microsoft.Quota provider"
    status: TransferStatus = TransferStatus.PENDING
    error: str = ""
    started_at: float | None = None
    completed_at: float | None = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "displayName": self.display_name,
            "status": self.status.value,
            "error": self.error,
            "startedAt": self.started_at,
            "completedAt": self.completed_at,
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
    pre_steps: list[PreStep] = field(default_factory=list)
    state: JobState = JobState.QUEUED
    started_at: float | None = None     # time.time() when job started running
    completed_at: float | None = None   # time.time() when job finished
    # Subscribers waiting for updates (SSE connections)
    _update_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    # Pause control: when set, the loop blocks after the current SKU completes
    _pause_requested: bool = field(default=False, repr=False)
    _resume_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    # Callback fired when a step (pre-step or SKU) completes or fails
    _on_step_complete: Any = field(default=None, repr=False)

    def pause(self):
        """Request pause — takes effect after the current SKU finishes.

        Immediately sets state to PAUSED for UI feedback.
        """
        self._pause_requested = True
        self._resume_event.clear()
        if self.state == JobState.RUNNING:
            self.state = JobState.PAUSED
            self.notify_update()

    def resume(self):
        """Resume a paused job."""
        self._pause_requested = False
        if self.state == JobState.PAUSED:
            self.state = JobState.RUNNING
            self.notify_update()
        self._resume_event.set()

    @property
    def is_pause_requested(self) -> bool:
        return self._pause_requested

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

        # Compute average duration of completed SKUs for ETA
        completed_durations = []
        for s in self.skus:
            if s.started_at and s.completed_at:
                completed_durations.append(s.completed_at - s.started_at)
        avg_duration = (
            sum(completed_durations) / len(completed_durations)
            if completed_durations
            else None
        )

        # Estimate remaining seconds
        remaining_count = pending + in_progress
        eta_seconds = None
        if avg_duration is not None and remaining_count > 0:
            # For the in-progress SKU, estimate remaining time based on how long it's been running
            in_prog_remaining = 0
            for s in self.skus:
                if s.status == TransferStatus.IN_PROGRESS and s.started_at:
                    elapsed = time.time() - s.started_at
                    in_prog_remaining = max(0, avg_duration - elapsed)
                    break
            eta_seconds = in_prog_remaining + (pending * avg_duration)

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
            "startedAt": self.started_at,
            "completedAt": self.completed_at,
            "avgSkuDuration": avg_duration,
            "etaSeconds": eta_seconds,
            "preSteps": [s.to_dict() for s in self.pre_steps],
            "skus": [s.to_dict() for s in self.skus],
        }

    def to_serializable(self) -> dict:
        """Serialize the job for persistence (all SKU states preserved)."""
        return {
            "job_id": self.job_id,
            "management_group_id": self.management_group_id,
            "quota_group_name": self.quota_group_name,
            "subscription_id": self.subscription_id,
            "subscription_name": self.subscription_name,
            "location": self.location,
            "leave_amount": self.leave_amount,
            "state": self.state.value,
            "started_at": self.started_at,
            "pre_steps": [
                {
                    "name": s.name,
                    "display_name": s.display_name,
                    "status": s.status.value,
                    "error": s.error,
                    "started_at": s.started_at,
                    "completed_at": s.completed_at,
                }
                for s in self.pre_steps
            ],
            "skus": [
                {
                    "resource_name": s.resource_name,
                    "display_name": s.display_name,
                    "original_limit": s.original_limit,
                    "new_limit": s.new_limit,
                    "status": s.status.value,
                    "error": s.error,
                    "started_at": s.started_at,
                    "completed_at": s.completed_at,
                }
                for s in self.skus
            ],
        }

    @classmethod
    def from_serializable(cls, data: dict) -> "TransferJob":
        """Rebuild a TransferJob from saved JSON data.

        SKUs that were in_progress when saved are reset to pending (they need to retry).
        """
        pre_steps = []
        for s in data.get("pre_steps", []):
            status = TransferStatus(s["status"])
            if status == TransferStatus.IN_PROGRESS:
                status = TransferStatus.PENDING
            pre_steps.append(
                PreStep(
                    name=s["name"],
                    display_name=s["display_name"],
                    status=status,
                    error=s.get("error", ""),
                    started_at=s.get("started_at") if status != TransferStatus.PENDING else None,
                    completed_at=s.get("completed_at"),
                )
            )

        skus = []
        for s in data["skus"]:
            status = TransferStatus(s["status"])
            # Reset in-progress to pending — the PATCH may or may not have applied
            if status == TransferStatus.IN_PROGRESS:
                status = TransferStatus.PENDING
            skus.append(
                SkuTransferResult(
                    resource_name=s["resource_name"],
                    display_name=s["display_name"],
                    original_limit=s["original_limit"],
                    new_limit=s["new_limit"],
                    status=status,
                    error=s.get("error", ""),
                    started_at=s.get("started_at") if status != TransferStatus.PENDING else None,
                    completed_at=s.get("completed_at"),
                )
            )

        job = cls(
            job_id=data["job_id"],
            management_group_id=data["management_group_id"],
            quota_group_name=data["quota_group_name"],
            subscription_id=data["subscription_id"],
            subscription_name=data["subscription_name"],
            location=data["location"],
            leave_amount=data["leave_amount"],
            skus=skus,
            pre_steps=pre_steps,
            state=JobState.PAUSED,  # Loaded jobs start paused
            started_at=data.get("started_at"),
        )
        job._pause_requested = True
        return job


async def execute_transfer(job: TransferJob) -> None:
    """Execute all SKU transfers sequentially in the background.

    Runs pre-steps (provider registration, group membership) first,
    then processes SKUs. Respects pause requests between steps.
    """
    job.state = JobState.RUNNING
    job.started_at = job.started_at or time.time()
    job.notify_update()

    # ─── Pre-steps ────────────────────────────────────────────────────────
    for step in job.pre_steps:
        if step.status in (TransferStatus.SUCCEEDED, TransferStatus.FAILED):
            continue

        # Check for pause
        if job._pause_requested:
            job.state = JobState.PAUSED
            job.notify_update()
            await job._resume_event.wait()
            # resume() already sets state to RUNNING
            job.notify_update()

        step.status = TransferStatus.IN_PROGRESS
        step.started_at = time.time()
        job.notify_update()

        try:
            if step.name == "register_provider":
                await _run_register_provider(job.subscription_id)
            elif step.name == "add_to_group":
                await _run_add_to_group(
                    job.management_group_id,
                    job.quota_group_name,
                    job.subscription_id,
                )
            step.status = TransferStatus.SUCCEEDED
        except AzRestError as e:
            # Provider registration failures are non-blocking (may already be registered)
            if step.name == "register_provider":
                logger.warning("Provider registration failed (non-blocking): %s", e)
                step.status = TransferStatus.SUCCEEDED
                step.error = f"Non-blocking: {e}"
            else:
                step.status = TransferStatus.FAILED
                step.error = str(e)
        except Exception as e:
            step.status = TransferStatus.FAILED
            step.error = f"Unexpected error: {e}"

        step.completed_at = time.time()
        job.notify_update()

        # Fire callback on step completion
        if job._on_step_complete:
            job._on_step_complete(job)

        # If adding to group failed, abort the whole job
        if step.name == "add_to_group" and step.status == TransferStatus.FAILED:
            job.state = JobState.COMPLETED
            job.completed_at = time.time()
            job.notify_update()
            return

    # ─── SKU Transfers ────────────────────────────────────────────────────
    for sku in job.skus:
        # Skip already-completed SKUs (relevant for resumed jobs)
        if sku.status in (TransferStatus.SUCCEEDED, TransferStatus.FAILED):
            continue

        # Check for pause request before starting next SKU
        if job._pause_requested:
            job.state = JobState.PAUSED
            job.notify_update()
            logger.info("Job %s paused — waiting for resume", job.job_id)
            await job._resume_event.wait()
            # resume() already sets state to RUNNING
            job.notify_update()
            logger.info("Job %s resumed", job.job_id)

        sku.status = TransferStatus.IN_PROGRESS
        sku.started_at = time.time()
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
                job.compute_usages_url, sku.resource_name, sku.new_limit, sku, job
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

        sku.completed_at = time.time()
        job.notify_update()

        # Fire callback on step completion
        if job._on_step_complete:
            job._on_step_complete(job)

    job.state = JobState.COMPLETED
    job.completed_at = time.time()
    job.notify_update()

    # Fire callback on job completion
    if job._on_step_complete:
        job._on_step_complete(job)


async def _run_register_provider(subscription_id: str) -> None:
    """Register Microsoft.Quota provider on the subscription (idempotent)."""
    check_url = (
        f"https://management.azure.com/subscriptions/{subscription_id}"
        f"/providers/Microsoft.Quota?api-version=2021-04-01"
    )
    result = await az_rest("get", check_url)
    state = result.get("registrationState", "NotRegistered")

    if state == "Registered":
        return

    register_url = (
        f"https://management.azure.com/subscriptions/{subscription_id}"
        f"/providers/Microsoft.Quota/register?api-version=2021-04-01"
    )
    await az_rest("post", register_url)

    # Poll until registered (up to 60s)
    for _ in range(12):
        await asyncio.sleep(5)
        result = await az_rest("get", check_url)
        if result.get("registrationState") == "Registered":
            return

    raise AzRestError("Microsoft.Quota provider registration timed out")


async def _run_add_to_group(
    management_group_id: str,
    quota_group_name: str,
    subscription_id: str,
) -> None:
    """Ensure subscription is a member of the quota group (idempotent PUT)."""
    url = (
        f"https://management.azure.com/providers/Microsoft.Management"
        f"/managementGroups/{management_group_id}"
        f"/providers/Microsoft.Quota/groupQuotas/{quota_group_name}"
        f"/subscriptions/{subscription_id}"
        f"?api-version={API_VERSION}"
    )

    # Check if already a member
    try:
        await az_rest("get", url)
        return  # Already a member
    except AzRestError:
        pass

    # Add subscription
    await az_rest("put", url)

    # Poll until provisioned (up to 60s)
    for _ in range(12):
        await asyncio.sleep(5)
        try:
            result = await az_rest("get", url)
            if result.get("properties", {}).get("provisioningState") == "Succeeded":
                return
        except AzRestError:
            pass

    raise AzRestError("Adding subscription to quota group timed out")


async def _poll_allocation_sequential(
    url: str, resource_name: str, expected_limit: int,
    sku: SkuTransferResult,
    job: TransferJob | None = None,
    max_polls: int = 20, interval: int = 15
) -> bool:
    """Poll the compute usages endpoint until the subscription limit matches expected.

    If the job is paused during polling, waits for resume before continuing.
    """
    # Initial wait before first poll
    await asyncio.sleep(20)

    for i in range(max_polls):
        # If paused, wait for resume before continuing to poll
        if job and job._pause_requested:
            logger.info("Poll paused for %s — waiting for resume", resource_name)
            await job._resume_event.wait()

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
