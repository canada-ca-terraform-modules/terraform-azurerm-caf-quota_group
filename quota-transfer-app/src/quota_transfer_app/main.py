"""FastAPI application for Azure quota transfer."""

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from .az_rest import AzRestError, az_rest, throttle_state, get_rate_stats, save_rate_state, load_rate_state
from .transfer import (
    JobState,
    PreStep,
    SkuTransferResult,
    TransferJob,
    TransferStatus,
    execute_transfer,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Azure Quota Transfer", version="0.2.0")

# In-memory store for all transfer jobs (persists for app lifetime)
_jobs: dict[str, TransferJob] = {}

API_VERSION = "2025-03-01"


@app.on_event("startup")
async def on_startup():
    """Load learned rate limits and any saved queue from previous session."""
    load_rate_state()
    await load_queue_internal()


@app.on_event("shutdown")
async def on_shutdown():
    """Persist learned rate limits for next session."""
    save_rate_state()


# ─── Models ───────────────────────────────────────────────────────────────────


class TransferRequest(BaseModel):
    management_group_id: str
    quota_group_name: str
    subscription_id: str
    subscription_name: str
    location: str
    leave_amount: int
    skus: list[dict[str, Any]]  # [{resource_name, display_name, current_limit}]


# ─── API Endpoints ────────────────────────────────────────────────────────────


@app.get("/api/management-groups")
async def list_management_groups():
    """List all management groups accessible to the current user."""
    try:
        url = "https://management.azure.com/providers/Microsoft.Management/managementGroups?api-version=2021-04-01"
        result = await az_rest("get", url)
        groups = []
        for mg in result.get("value", []):
            groups.append(
                {
                    "id": mg.get("name", ""),
                    "displayName": mg.get("properties", {}).get("displayName", mg.get("name", "")),
                }
            )
        return {"managementGroups": groups}
    except AzRestError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/quota-groups/{management_group_id}")
async def list_quota_groups(management_group_id: str):
    """List quota groups in a management group."""
    try:
        url = (
            f"https://management.azure.com/providers/Microsoft.Management"
            f"/managementGroups/{management_group_id}"
            f"/providers/Microsoft.Quota/groupQuotas"
            f"?api-version={API_VERSION}"
        )
        result = await az_rest("get", url)
        groups = []
        for qg in result.get("value", []):
            groups.append(
                {
                    "name": qg.get("name", ""),
                    "id": qg.get("id", ""),
                    "provisioningState": qg.get("properties", {}).get("provisioningState", ""),
                }
            )
        return {"quotaGroups": groups}
    except AzRestError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/subscriptions")
async def list_subscriptions():
    """List all subscriptions accessible to the current user (follows pagination)."""
    try:
        url = "https://management.azure.com/subscriptions?api-version=2022-12-01"
        subs = []

        while url:
            result = await az_rest("get", url)
            for sub in result.get("value", []):
                subs.append(
                    {
                        "subscriptionId": sub.get("subscriptionId", ""),
                        "displayName": sub.get("displayName", ""),
                        "state": sub.get("state", ""),
                    }
                )
            url = result.get("nextLink", None)

        return {"subscriptions": subs}
    except AzRestError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/group-quotas/{management_group_id}/{quota_group_name}/{location}")
async def get_group_quotas(management_group_id: str, quota_group_name: str, location: str):
    """Get the quota group's limits per SKU for a location."""
    try:
        url = (
            f"https://management.azure.com/providers/Microsoft.Management"
            f"/managementGroups/{management_group_id}"
            f"/providers/Microsoft.Quota/groupQuotas/{quota_group_name}"
            f"/resourceProviders/Microsoft.Compute"
            f"/groupQuotaLimits/{location}"
            f"?api-version={API_VERSION}"
        )
        result = await az_rest("get", url)
        # Response is nested: result.properties.value[]
        props = result.get("properties", {})
        values = props.get("value", [])
        quotas = {}

        # Follow pagination
        while True:
            for v in values:
                item_props = v.get("properties", {})
                resource_name = item_props.get("resourceName", "")
                if resource_name:
                    # Calculate total group quota from availableLimit + allocated out
                    available = item_props.get("availableLimit", 0)
                    allocated_subs = item_props.get("allocatedToSubscriptions", {}).get("value", [])
                    total_allocated_out = sum(
                        a.get("quotaAllocated", 0)
                        for a in allocated_subs
                        if a.get("quotaAllocated", 0) > 0
                    )
                    total_in_group = available + total_allocated_out
                    quotas[resource_name] = {
                        "availableLimit": available,
                        "groupQuota": total_in_group,
                        "allocatedOut": total_allocated_out,
                    }
            next_link = props.get("nextLink")
            if not next_link:
                break
            result = await az_rest("get", next_link)
            props = result.get("properties", result)
            values = props.get("value", [])

        return {"groupQuotas": quotas}
    except AzRestError as e:
        # If the endpoint doesn't exist or fails, return empty (non-blocking)
        logger.warning("Failed to fetch group quotas: %s", e)
        return {"groupQuotas": {}}


@app.post("/api/register-quota-provider/{subscription_id}")
async def register_quota_provider(subscription_id: str):
    """Check if Microsoft.Quota is registered; if not, register it and poll until ready."""
    try:
        # Check current registration state
        check_url = (
            f"https://management.azure.com/subscriptions/{subscription_id}"
            f"/providers/Microsoft.Quota?api-version=2021-04-01"
        )
        result = await az_rest("get", check_url)
        state = result.get("registrationState", "NotRegistered")

        if state == "Registered":
            return {"status": "already_registered", "registrationState": state}

        # Register the provider
        register_url = (
            f"https://management.azure.com/subscriptions/{subscription_id}"
            f"/providers/Microsoft.Quota/register?api-version=2021-04-01"
        )
        await az_rest("post", register_url)

        # Poll until registered (up to 60s)
        for i in range(12):
            await asyncio.sleep(5)
            result = await az_rest("get", check_url)
            state = result.get("registrationState", "")
            if state == "Registered":
                return {"status": "registered", "registrationState": state}

        return {"status": "registering", "registrationState": state, "message": "Still registering, try again in a moment"}

    except AzRestError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/ensure-subscription-in-group")
async def ensure_subscription_in_group(
    management_group_id: str = "",
    quota_group_name: str = "",
    subscription_id: str = "",
):
    """Ensure a subscription is a member of the quota group (idempotent PUT)."""
    try:
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
            return {"status": "already_member"}
        except AzRestError:
            pass  # Not a member yet, proceed to add

        # Add subscription to the quota group
        await az_rest("put", url)

        # Poll until provisioned (up to 60s)
        for i in range(12):
            await asyncio.sleep(5)
            try:
                result = await az_rest("get", url)
                state = result.get("properties", {}).get("provisioningState", "")
                if state == "Succeeded":
                    return {"status": "registered", "provisioningState": state}
            except AzRestError:
                pass

        return {"status": "registering", "message": "Still provisioning, try again in a moment"}

    except AzRestError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/compute-usages/{subscription_id}/{location}")
async def get_compute_usages(subscription_id: str, location: str):
    """Get compute quota usages for a subscription in a location."""
    try:
        url = (
            f"https://management.azure.com/subscriptions/{subscription_id}"
            f"/providers/Microsoft.Compute/locations/{location}"
            f"/usages?api-version=2023-07-01"
        )
        result = await az_rest("get", url)
        usages = []
        for usage in result.get("value", []):
            name_obj = usage.get("name", {})
            usages.append(
                {
                    "resourceName": name_obj.get("value", ""),
                    "displayName": name_obj.get("localizedValue", ""),
                    "currentUsage": usage.get("currentValue", 0),
                    "limit": usage.get("limit", 0),
                }
            )
        # Sort by display name
        usages.sort(key=lambda x: x["displayName"].lower())
        return {"usages": usages}
    except AzRestError as e:
        raise HTTPException(status_code=502, detail=str(e))


# ─── Job Queue Endpoints ──────────────────────────────────────────────────────


@app.get("/api/jobs")
async def list_jobs():
    """Return all jobs (newest first)."""
    jobs = list(_jobs.values())
    jobs.reverse()
    return {"jobs": [j.summary() for j in jobs]}


@app.post("/api/transfer")
async def start_transfer(req: TransferRequest):
    """Start a batch quota transfer (runs in background).

    Includes pre-steps: register Microsoft.Quota provider and add subscription to group.
    """
    job_id = str(uuid.uuid4())

    # Build SKU results
    sku_results = []
    for sku in req.skus:
        current_limit = sku["current_limit"]
        new_limit = req.leave_amount  # the amount to leave in the subscription

        if new_limit >= current_limit:
            continue  # nothing to transfer

        sku_results.append(
            SkuTransferResult(
                resource_name=sku["resource_name"],
                display_name=sku["display_name"],
                original_limit=current_limit,
                new_limit=new_limit,
            )
        )

    if not sku_results:
        raise HTTPException(
            status_code=400,
            detail="No SKUs to transfer (leave amount >= current limit for all selected SKUs)",
        )

    # Build pre-steps
    pre_steps = [
        PreStep(
            name="register_provider",
            display_name="Register Microsoft.Quota provider",
        ),
        PreStep(
            name="add_to_group",
            display_name=f"Add subscription to quota group '{req.quota_group_name}'",
        ),
    ]

    job = TransferJob(
        job_id=job_id,
        management_group_id=req.management_group_id,
        quota_group_name=req.quota_group_name,
        subscription_id=req.subscription_id,
        subscription_name=req.subscription_name,
        location=req.location,
        leave_amount=req.leave_amount,
        skus=sku_results,
        pre_steps=pre_steps,
    )
    job._on_step_complete = _on_job_progress
    _jobs[job_id] = job

    # Auto-save: persist queue immediately when a new job is added
    asyncio.create_task(save_queue_internal())

    # Fire-and-forget: run the transfer in the background
    asyncio.create_task(_run_transfer(job))

    return {"jobId": job_id, "skuCount": len(sku_results)}


async def _run_transfer(job: TransferJob):
    """Background task that executes a transfer job."""
    try:
        await execute_transfer(job)
    except Exception as e:
        logger.exception("Transfer job %s crashed: %s", job.job_id, e)
        job.state = JobState.COMPLETED
        job.notify_update()


# ─── Auto-save on progress ────────────────────────────────────────────────────

_queue_save_scheduled: bool = False


def _on_job_progress(job: TransferJob):
    """Called after every step/SKU completes or fails. Triggers debounced queue save."""
    global _queue_save_scheduled
    if _queue_save_scheduled:
        return
    _queue_save_scheduled = True

    async def _do_save():
        global _queue_save_scheduled
        await save_queue_internal()
        _queue_save_scheduled = False

    try:
        asyncio.get_event_loop().create_task(_do_save())
    except RuntimeError:
        _queue_save_scheduled = False


@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str, request: Request):
    """SSE stream for a specific job's progress. Read-only view of background state."""
    if job_id not in _jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    job = _jobs[job_id]

    async def event_generator():
        # Always send full current state first
        snapshot = job.summary()
        snapshot["throttle"] = dict(throttle_state)
        snapshot["rateStats"] = get_rate_stats()
        yield {
            "event": "snapshot",
            "data": json.dumps(snapshot),
        }

        # If already done, close immediately
        if job.state == JobState.COMPLETED:
            return

        # Stream incremental updates until job completes
        while job.state != JobState.COMPLETED:
            if await request.is_disconnected():
                break
            await job.wait_for_update(timeout=2.0)
            snapshot = job.summary()
            snapshot["throttle"] = dict(throttle_state)
            snapshot["rateStats"] = get_rate_stats()
            yield {
                "event": "snapshot",
                "data": json.dumps(snapshot),
            }

        # Final snapshot
        snapshot = job.summary()
        snapshot["throttle"] = dict(throttle_state)
        snapshot["rateStats"] = get_rate_stats()
        yield {
            "event": "snapshot",
            "data": json.dumps(snapshot),
        }

    return EventSourceResponse(event_generator())


@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str):
    """Remove a job from the queue. Running jobs are paused first."""
    if job_id not in _jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    job = _jobs[job_id]

    # If running, pause it first (current SKU will finish, then it stops)
    if job.state == JobState.RUNNING:
        job.pause()

    del _jobs[job_id]
    asyncio.create_task(save_queue_internal())
    return {"status": "deleted", "jobId": job_id}


@app.delete("/api/jobs/by-subscription/{subscription_id}")
async def delete_jobs_by_subscription(subscription_id: str):
    """Remove all jobs for a given subscription. Running jobs are paused first."""
    deleted = []
    for job_id in list(_jobs.keys()):
        job = _jobs[job_id]
        if job.subscription_id == subscription_id:
            if job.state == JobState.RUNNING:
                job.pause()
            del _jobs[job_id]
            deleted.append(job_id)

    if deleted:
        asyncio.create_task(save_queue_internal())

    return {"status": "deleted", "count": len(deleted), "jobIds": deleted}


@app.delete("/api/jobs/{job_id}/skus/{resource_name}")
async def delete_job_sku(job_id: str, resource_name: str):
    """Remove a pending SKU from a job. Only pending SKUs can be removed."""
    if job_id not in _jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    job = _jobs[job_id]
    original_count = len(job.skus)
    job.skus = [s for s in job.skus if not (s.resource_name == resource_name and s.status == TransferStatus.PENDING)]

    if len(job.skus) == original_count:
        raise HTTPException(status_code=404, detail="Pending SKU not found")

    # If no pending/in-progress SKUs left, mark job as completed
    remaining_active = [s for s in job.skus if s.status in (TransferStatus.PENDING, TransferStatus.IN_PROGRESS)]
    if not remaining_active and job.state != JobState.COMPLETED:
        job.state = JobState.COMPLETED
        job.completed_at = time.time()

    job.notify_update()
    asyncio.create_task(save_queue_internal())
    return {"status": "deleted", "resourceName": resource_name, "remainingSkus": len(job.skus)}


# ─── Pause / Resume ──────────────────────────────────────────────────────────

SAVE_PATH = Path("/tmp/quota-transfer-queue.json")


@app.post("/api/queue/pause")
async def pause_queue():
    """Pause all running jobs (takes effect after current SKU finishes). Auto-saves state."""
    paused_count = 0
    for job in _jobs.values():
        if job.state in (JobState.RUNNING, JobState.QUEUED):
            job.pause()
            paused_count += 1

    # Auto-save on pause so state is recoverable if app is killed
    if paused_count > 0:
        await save_queue_internal()
        save_rate_state()

    return {"status": "paused", "paused": paused_count}


@app.post("/api/queue/resume")
async def resume_queue():
    """Resume all paused jobs."""
    resumed_count = 0
    for job in _jobs.values():
        if job.state == JobState.PAUSED:
            job.resume()
            resumed_count += 1
        elif job.state == JobState.QUEUED:
            # Loaded jobs that were never started — kick them off
            job.resume()
            asyncio.create_task(_run_transfer(job))
            resumed_count += 1
    return {"status": "resumed", "resumed": resumed_count}


@app.post("/api/queue/save")
async def save_queue():
    """Save all non-completed jobs to a temp file for later resumption."""
    result = await save_queue_internal()
    return result


async def save_queue_internal():
    """Internal: persist queue state to disk."""
    jobs_to_save = []
    for job in _jobs.values():
        jobs_to_save.append(job.to_serializable())

    SAVE_PATH.write_text(json.dumps(jobs_to_save, indent=2))
    save_rate_state()
    logger.info("Saved %d jobs to %s", len(jobs_to_save), SAVE_PATH)
    return {
        "status": "saved",
        "path": str(SAVE_PATH),
        "jobCount": len(jobs_to_save),
    }


@app.post("/api/queue/load")
async def load_queue():
    """Load previously saved queue from the temp file.

    Loaded jobs come back in paused state — call /api/queue/resume to continue.
    """
    if not SAVE_PATH.exists():
        raise HTTPException(status_code=404, detail=f"No saved queue found at {SAVE_PATH}")

    result = await load_queue_internal()
    if result is None:
        raise HTTPException(status_code=500, detail="Failed to load queue")
    return result


async def load_queue_internal() -> dict | None:
    """Internal: load saved queue from disk. Returns None if no save file exists."""
    if not SAVE_PATH.exists():
        logger.info("No saved queue at %s — starting fresh", SAVE_PATH)
        return None

    try:
        data = json.loads(SAVE_PATH.read_text())
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to read save file %s: %s", SAVE_PATH, e)
        return None

    loaded_count = 0
    skipped_count = 0
    for job_data in data:
        job_id = job_data["job_id"]
        if job_id in _jobs:
            skipped_count += 1
            continue  # Don't overwrite active jobs

        job = TransferJob.from_serializable(job_data)

        # If the saved job was already completed, load as-is
        if job_data.get("state") == "completed":
            job.state = JobState.COMPLETED
            job._pause_requested = False

        job._on_step_complete = _on_job_progress
        _jobs[job_id] = job
        loaded_count += 1

        # For non-completed jobs, start their background task (paused)
        if job.state != JobState.COMPLETED:
            asyncio.create_task(_run_transfer(job))

    logger.info("Loaded %d jobs from %s (skipped %d duplicates)", loaded_count, SAVE_PATH, skipped_count)
    return {
        "status": "loaded",
        "path": str(SAVE_PATH),
        "loaded": loaded_count,
        "skipped": skipped_count,
    }


# ─── Rate Limiting Stats ──────────────────────────────────────────────────────


@app.get("/api/rate-stats")
async def rate_stats():
    """Return current adaptive rate-limiting stats for all endpoint buckets."""
    return get_rate_stats()


# ─── Serve the frontend ───────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def index():
    """Serve the single-file HTML frontend."""
    html_path = Path(__file__).parent / "static" / "index.html"
    return HTMLResponse(html_path.read_text())
