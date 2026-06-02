# Azure deployment — Container Apps (Consumption)

The most cost-effective production path: scales to zero when idle, ~$5/mo for ACR, <$1/mo per weekly run.

## Why not Functions / App Service?

| Option | Idle/mo | Run/mo (5 min/week) | Timeout | Verdict |
|---|---|---|---|---|
| Functions Consumption | $0 | ~$0.10 | **10 min** | Run easily exceeds 10 min once you process 100+ events |
| Functions Premium EP1 | $160 | $0.10 | unbounded | Way too expensive for a weekly job |
| App Service B1 | $13 | $0 | unbounded | Flat $13/mo regardless of usage |
| **Container Apps Consumption** | **$0** | **~$0.30** | **unbounded** | **Winner** |
| LangGraph Cloud | $0 + per-trace | varies | unbounded | Easiest, but vendor lock-in |

Container Apps hosts the LangGraph runtime while PostgreSQL stores durable
checkpoints. `MemorySaver` is kept for local development only; it cannot
survive a restart or scale-to-zero cycle.

## One-shot deploy

```powershell
# Required env vars (set before running):
#   EITHER Opencode Go (international, OpenAI-compatible):
$env:OPENCODE_GO_API_KEY     = "..."
$env:OPENCODE_GO_MODEL       = "glm-5.1"   # or kimi-k2.5, deepseek-v4-pro, ...
#   OR Azure OpenAI (fallback when Opencode Go is absent):
$env:AZURE_OPENAI_API_KEY    = "..."
$env:AZURE_OPENAI_ENDPOINT   = "https://myoai.openai.azure.com/"
$env:GRAPH_TENANT_ID         = "..."
$env:GRAPH_CLIENT_ID         = "..."
$env:GRAPH_CLIENT_SECRET     = "..."
$env:GRAPH_USER_ID           = "user@contoso.com"
$env:ONEDRIVE_DRIVE_ID       = "b!abc..."
$env:SEC_USER_AGENT          = "Your Name you@example.com"   # SEC requires a real contact
$env:CHECKPOINT_POSTGRES_URI = "postgresql://user:password@host:5432/calorch?sslmode=require"
$env:CALORCH_API_KEY         = "<generate-a-random-32-byte-secret>"

# Optional: LangSmith tracing
$env:LANGSMITH_API_KEY       = "lsv2_..."

# Deploy (creates RG, ACR, Cosmos, Container App, managed identity, role assignment):
.\deploy\deploy.ps1
```

The script will:
1. `az login` if needed
2. Create resource group `rg-calorch-prod` in `eastus`
3. Create ACR (Basic, $5/mo)
4. Create Cosmos DB (Serverless, $0.25/GB)
5. Create Container Apps Environment + Log Analytics
6. `az acr build` — builds and pushes the image
7. Deploy the Container App with all env vars and secrets wired
8. Print the FQDN

`CHECKPOINT_POSTGRES_URI` points to an existing PostgreSQL database. The
service calls LangGraph `PostgresSaver.setup()` at startup to create its
checkpoint tables.

## Triggering runs

The Container App has a built-in **cron scale rule** that fires every Monday at 06:00 UTC:

```yaml
- name: weekly-cron
  type: cron
  metadata:
    azFunctionScheduler: {"triggerType": "Schedule", "cronExpression": "0 6 * * MON"}
```

To trigger on demand:

```powershell
$url = "https://calorch.<region>.azurecontainerapps.io"
$body = @{
    start = "2026-06-01T00:00:00Z"
    end   = "2026-06-08T00:00:00Z"
    send_emails = $false        # true to actually send
} | ConvertTo-Json

$headers = @{ "X-Calorch-API-Key" = $env:CALORCH_API_KEY }
Invoke-RestMethod -Method Post -Uri "$url/run" -Headers $headers -Body $body -ContentType "application/json"
```

When `send_emails=true`, the response is `pending_approval` after previews
are generated. Resume the same thread explicitly:

```powershell
Invoke-RestMethod -Method Post `
  -Uri "$url/runs/$threadId/approval" `
  -Headers $headers `
  -ContentType "application/json" `
  -Body '{"approved":true}'
```

## Cost breakdown (typical weekly run)

| Resource | Quantity | Unit cost | Monthly |
|---|---|---|---|
| ACR (Basic) | 1 | $5.00 | **$5.00** |
| Container Apps | 0 idle, ~20 min/week active | $0.000012/vCPU-sec | **~$0.30** |
| Cosmos DB Serverless | 100 RUs, ~10 MB stored | $0.25/GB | **~$0.05** |
| PostgreSQL checkpoints | Existing shared Azure Database for PostgreSQL | shared | **existing** |
| Log Analytics | 1 GB ingested | $2.30/GB | **~$2.30** |
| Application Insights | 1 GB | $2.30/GB | (subset of LA) |
| **Total** | | | **~$7.65/mo** |

## Local dev loop

```powershell
# Build locally
docker build -t calorch:dev .

# Run with .env.dev (USE_MOCKS=true)
docker run --rm -p 8000:8000 -v ${PWD}/.env:/app/.env:ro calorch:dev

# Or run the CLI directly (no Docker):
python -m calorch.cli run --start 2026-03-02 --end 2026-03-09
```

## Tear down

```powershell
az group delete --name rg-calorch-prod --yes --no-wait
```

Stops all billing within a minute.
