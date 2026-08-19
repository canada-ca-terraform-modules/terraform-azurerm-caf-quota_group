"""FastAPI application for Azure quota transfer."""

import asyncio
import json
import logging
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from .az_rest import AzRestError, az_rest
from .transfer import (
    JobState,
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
    """Start a batch quota transfer (runs in background)."""
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

    job = TransferJob(
        job_id=job_id,
        management_group_id=req.management_group_id,
        quota_group_name=req.quota_group_name,
        subscription_id=req.subscription_id,
        subscription_name=req.subscription_name,
        location=req.location,
        leave_amount=req.leave_amount,
        skus=sku_results,
    )
    _jobs[job_id] = job

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


@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str, request: Request):
    """SSE stream for a specific job's progress. Read-only view of background state."""
    if job_id not in _jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    job = _jobs[job_id]

    async def event_generator():
        # Always send full current state first
        yield {
            "event": "snapshot",
            "data": json.dumps(job.summary()),
        }

        # If already done, close immediately
        if job.state == JobState.COMPLETED:
            return

        # Stream incremental updates until job completes
        while job.state != JobState.COMPLETED:
            if await request.is_disconnected():
                break
            await job.wait_for_update(timeout=2.0)
            yield {
                "event": "snapshot",
                "data": json.dumps(job.summary()),
            }

        # Final snapshot
        yield {
            "event": "snapshot",
            "data": json.dumps(job.summary()),
        }

    return EventSourceResponse(event_generator())


@app.delete("/api/jobs/{job_id}")
async def dismiss_job(job_id: str):
    """Remove a completed job from the list."""
    if job_id not in _jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    job = _jobs[job_id]
    if job.state != JobState.COMPLETED:
        raise HTTPException(status_code=409, detail="Cannot dismiss a running job")
    del _jobs[job_id]
    return {"status": "dismissed"}


# ─── Serve the frontend ───────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def index():
    """Serve the single-file HTML frontend."""
    html_path = Path(__file__).parent / "static" / "index.html"
    return HTMLResponse(html_path.read_text())
