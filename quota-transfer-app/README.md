# Quota Transfer App

Transfer compute quota from Azure subscriptions into Quota Groups — Azure Portal–style UI.

## Quick Start

```bash
# 1. Make sure you have uv installed (https://docs.astral.sh/uv/getting-started/installation/)
# 2. Log in to Azure — use the tenant that contains your Quota Groups
az login --tenant <your-tenant-id>

# 3. Run the app
cd quota-transfer-app
./run.sh
```

Open http://localhost:8000

That's it. The `run.sh` script creates a Python virtual environment and installs dependencies automatically on first run.

## Prerequisites

| Requirement | Why |
|-------------|-----|
| [uv](https://docs.astral.sh/uv/) | Python package manager (installs deps) |
| Azure CLI (`az`) | All API calls go through `az rest` |
| `az login` session | Uses your logged-in identity |

### Required Permissions

- **Reader** on management groups (to list quota groups)
- **Quota Request Operator** on the target subscription
- **GroupQuota Request Operator** on the management group

The app will automatically register `Microsoft.Quota` provider and add the subscription to the quota group if needed.

## How It Works

1. Select a **Management Group** and **Quota Group**
2. Select a **Subscription** (supports `*` wildcard filter, e.g. `*AAFC*DP`)
3. Choose a **Region** (defaults to `canadacentral`)
4. Click **Load Quotas**
5. Filter SKUs (supports `*` wildcards), check the ones to transfer
6. Set **"Leave in sub"** — the quota each SKU will keep (rest goes to group)
7. Click **Transfer Selected** — transfers happen one at a time with live progress

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Empty dropdowns / 502 errors | Run `az login` — your session expired |
| "RequestThrottled" errors | Wait 5 minutes — Azure rate-limits quota API calls. The app retries automatically |
| Transfer never completes | The API can take 30-60s per SKU. The app polls for up to 5 minutes per SKU |
| Permission errors | Check RBAC: Quota Request Operator on sub, GroupQuota Request Operator on MG |

## Architecture

```
quota-transfer-app/
├── run.sh                           # Run this ← 
├── pyproject.toml                   # Python dependencies
└── src/quota_transfer_app/
    ├── main.py                      # FastAPI app + API endpoints
    ├── az_rest.py                   # Async az CLI wrapper (3 concurrent, auto-retry)
    ├── transfer.py                  # Sequential transfer logic + polling
    └── static/
        └── index.html               # Single-file frontend
```
