# Deploying calorch to Azure — Durable Functions

This is the deployment guide for calorch: an Azure **Durable Functions**
app with LangGraph multi-agent subgraphs running inside activities.

```
                        ┌────────────────────────────────────────────┐
  Timer (CRON_SCHEDULE) │  Function App (Python 3.11, Linux)         │
  POST /api/run ───────►│  calorch_orchestrator (Durable Functions)  │
  POST /api/approval/{id}    │ scan → classify → agents ×N → gate    │
  GET  /api/status/{id} │    │ → deliver ×N → briefing               │
                        │  activities invoke LangGraph agents        │
                        └──────┬──────────────┬──────────────┬───────┘
                               │              │              │
                        Storage account   Azure OpenAI   Microsoft Graph
                        (task hub +       (classify +    (calendar, mail,
                        calorch-inputs/   enrichment)    OneDrive)
                        calorch-outputs)
```

---

## 1. Prerequisites

| Requirement | Notes |
|---|---|
| Azure subscription | Contributor on a resource group is enough |
| Azure CLI ≥ 2.60 | `az login` completed |
| Azure Functions Core Tools v4 | `func --version` → 4.x (needed for `func azure functionapp publish`) |
| Python 3.11 | Matches `pyproject.toml` (`azure-functions` is pinned `<2.0`, which requires Python ≤ 3.12) |
| Microsoft Graph app registration | Only for non-mock runs — see §6 |
| Azure OpenAI resource with a chat deployment | Only for non-mock runs (`gpt-4o` by default) |

Everything below uses bash; substitute your own names where marked.

```bash
# ---- choose your names once, reuse everywhere ----
RG=rg-calorch-prod
LOC=eastus
STG=stcalorch$RANDOM            # storage account: 3-24 lowercase alphanumerics, globally unique
APP=func-calorch-prod           # function app name, globally unique
PLAN_TYPE=flexconsumption       # see §2 for the plan decision
AI=appi-calorch-prod            # Application Insights
KV=kv-calorch-prod              # Key Vault (optional but recommended)
```

---

## 2. Choose a hosting plan

`host.json` sets `functionTimeout: 00:30:00`. That rules out the classic
Consumption plan, whose hard ceiling is **10 minutes** — a run that
processes 100+ events with live LLM calls will exceed it inside a single
activity.

| Plan | Verdict | Why |
|---|---|---|
| **Flex Consumption** (recommended) | ✅ | Scale to zero, per-execution billing, default timeout 30 min and configurable beyond; supports Python 3.11 and Durable Functions |
| Elastic Premium (EP1) | ✅ but ~$160/mo idle | Use only if you need VNet integration or pre-warmed instances |
| Classic Consumption (Y1) | ❌ | 10-minute timeout cap conflicts with `host.json` |
| Dedicated/App Service | ⚠️ | Works, flat cost; pointless for a weekly batch |

Durable Functions does not need anything beyond a storage account — the
task hub (`CalorchTaskHub`, configured in `host.json`) lives in Azure
Storage tables/queues/blobs under `AzureWebJobsStorage`.

---

## 3. Provision the infrastructure

```bash
az group create -n $RG -l $LOC

# Storage account — used BOTH for the Durable task hub and the
# calorch-inputs / calorch-outputs blob containers
az storage account create -n $STG -g $RG -l $LOC \
  --sku Standard_LRS --kind StorageV2 --min-tls-version TLS1_2 \
  --allow-blob-public-access false

az storage container create --account-name $STG -n calorch-inputs  --auth-mode login
az storage container create --account-name $STG -n calorch-outputs --auth-mode login

# Application Insights (workspace-based)
az monitor log-analytics workspace create -g $RG -n law-calorch -l $LOC
LAW_ID=$(az monitor log-analytics workspace show -g $RG -n law-calorch --query id -o tsv)
az monitor app-insights component create -g $RG --app $AI -l $LOC --workspace $LAW_ID
AI_CONN=$(az monitor app-insights component show -g $RG --app $AI \
  --query connectionString -o tsv)

# Function App — Flex Consumption, Python 3.11, system-assigned identity
az functionapp create -g $RG -n $APP \
  --flexconsumption-location $LOC \
  --runtime python --runtime-version 3.11 \
  --storage-account $STG \
  --assign-identity '[system]' \
  --app-insights $AI

# If Flex Consumption is unavailable in your region, fall back to EP1:
#   az functionapp plan create -g $RG -n plan-calorch --sku EP1 --is-linux
#   az functionapp create -g $RG -n $APP --plan plan-calorch \
#     --runtime python --runtime-version 3.11 --storage-account $STG \
#     --assign-identity '[system]' --functions-version 4
```

Grant the app's managed identity access to the blob containers (used by
`AZURE_STORAGE_ACCOUNT_URL` instead of a connection string — preferred):

```bash
PRINCIPAL_ID=$(az functionapp identity show -g $RG -n $APP --query principalId -o tsv)
STG_ID=$(az storage account show -n $STG -g $RG --query id -o tsv)

az role assignment create --assignee $PRINCIPAL_ID \
  --role "Storage Blob Data Contributor" --scope $STG_ID
```

---

## 4. Key Vault for secrets (recommended)

Store every secret in Key Vault and reference it from app settings, so
nothing sensitive sits in plain text in the portal:

```bash
az keyvault create -n $KV -g $RG -l $LOC --enable-rbac-authorization true
KV_ID=$(az keyvault show -n $KV -g $RG --query id -o tsv)

# let the function app read secrets
az role assignment create --assignee $PRINCIPAL_ID \
  --role "Key Vault Secrets User" --scope $KV_ID

# add your secrets (repeat per secret)
az keyvault secret set --vault-name $KV -n azure-openai-api-key  --value "<key>"
az keyvault secret set --vault-name $KV -n graph-client-secret   --value "<secret>"
az keyvault secret set --vault-name $KV -n tiingo-api-key        --value "<key>"
az keyvault secret set --vault-name $KV -n fred-api-key          --value "<key>"
```

A Key Vault reference in an app setting looks like:

```
@Microsoft.KeyVault(VaultName=kv-calorch-prod;SecretName=azure-openai-api-key)
```

---

## 5. Application settings

The app reads plain environment variables (see `src/calorch/config.py`
for the authoritative list and defaults). Set them with
`az functionapp config appsettings set -g $RG -n $APP --settings KEY=VALUE ...`.

### 5.1 Required — runtime & orchestration

| Setting | Value | Purpose |
|---|---|---|
| `AzureWebJobsStorage` | set automatically by `az functionapp create` | Durable task hub (`CalorchTaskHub`) + host bookkeeping |
| `CRON_SCHEDULE` | `0 0 9 * * 1` (default) | Timer trigger — NCRONTAB with seconds; default is Mondays 09:00 UTC. Read at import time, so changing it requires an app restart |
| `APPROVER_EMAILS` | CSV of approver addresses | Notified by email when a send run pauses at the gate, with a tokenized review-page link. Empty = no notification (gate still works via the keyed API) |
| `APPROVAL_BASE_URL` | *(optional)* | Base URL for emailed links; defaults to `https://$WEBSITE_HOSTNAME` |
| `OUTPUT_DIR` | `/tmp/calorch-out` | Artifact scratch dir. **Required on Azure**: the package mount is read-only, the code default `./out` will fail. Artifacts are persisted to blob storage; `/tmp` is fine as scratch |
| `SEC_CACHE_DIR` | `/tmp/calorch-cache/sec` | Same reason as above |
| `USE_MOCKS` | `false` for production, `true` to smoke-test without credentials | With `true` the app runs end-to-end on seed data and mock clients |

### 5.2 Blob persistence (inputs/outputs)

| Setting | Value | Purpose |
|---|---|---|
| `AZURE_STORAGE_ACCOUNT_URL` | `https://$STG.blob.core.windows.net` | Managed-identity blob access (preferred; requires the role assignment from §3) |
| `AZURE_STORAGE_CONNECTION_STRING` | *(alternative)* | Use **either** this or the account URL, not both |
| `BLOB_INPUT_CONTAINER` | `calorch-inputs` (default) | Pre-ingested provider data |
| `BLOB_OUTPUT_CONTAINER` | `calorch-outputs` (default) | DOCX/HTML/briefing artifacts |
| `USE_BLOB_PROVIDERS` | `true` (default) | Orchestrated runs read pre-ingested data from blob instead of hitting live APIs |

### 5.3 LLM (one of the two)

| Setting | Value |
|---|---|
| `AZURE_OPENAI_API_KEY` | `@Microsoft.KeyVault(VaultName=$KV;SecretName=azure-openai-api-key)` |
| `AZURE_OPENAI_ENDPOINT` | `https://<your-aoai>.openai.azure.com/` |
| `AZURE_OPENAI_DEPLOYMENT` | `gpt-4o` (default) |
| `AZURE_OPENAI_API_VERSION` | `2024-08-01-preview` (default) |
| — or — | |
| `OPENCODE_GO_API_KEY` | OpenAI-compatible alternative; overrides Azure OpenAI when set |
| `OPENCODE_GO_MODEL` | `glm-5.1` (default) |

### 5.4 Microsoft Graph (calendar, mail, OneDrive) — see §6

| Setting | Value |
|---|---|
| `GRAPH_TENANT_ID` | Entra tenant GUID |
| `GRAPH_CLIENT_ID` | App registration client ID |
| `GRAPH_CLIENT_SECRET` | Key Vault reference |
| `GRAPH_USER_ID` | UPN or object ID of the mailbox/calendar to operate as |
| `ONEDRIVE_DRIVE_ID` | Target drive for DOCX archive |

### 5.5 Repository backend (delivery idempotency)

| Setting | Value | Purpose |
|---|---|---|
| `REPO_BACKEND` | `table` for production (`json` writes to local disk — only safe with mocks/local dev) | Delivery-idempotency records. `json` on ephemeral `/tmp` is **not shared across scaled-out instances**, so it loses duplicate-send protection — use `table` in production |
| `REPO_TABLE_NAME` | `calorchdelivery` (default) | Azure Table created in the function app's existing storage account — no extra service, ~cents/mo. Uses the same `AZURE_STORAGE_*` credentials/identity as blob (§5.2) |

> Azure Table Storage replaces the former Cosmos DB backend for this job:
> the workload is point read/write by `event_id` with no cross-key
> contention, which Table serves at a fraction of the cost. Cosmos is only
> warranted if you later need rich queries over a research corpus — for
> that, prefer Azure AI Search (§5.6).

### 5.6 Institutional-knowledge RAG (Azure AI Search — optional)

Augments enrichment LLM calls with retrieved prior research and indexes
each prepared analysis. Leave `AZURE_SEARCH_ENDPOINT` empty to disable
(no-op). Provision: `az search service create -n <svc> -g $RG --sku basic`
(or `free` to pilot), then create an index named `calorch-knowledge` with
fields `id` (key), `content`/`title` (searchable), `event_id`,
`event_type`, `tickers` (Collection(Edm.String), filterable), `run_id`,
`confidence` (Edm.Double) — optionally a semantic configuration for
semantic ranking and a vector field + Azure OpenAI embedding skill for
vector/hybrid retrieval.

| Setting | Value | Purpose |
|---|---|---|
| `AZURE_SEARCH_ENDPOINT` | `https://<svc>.search.windows.net` | Enables RAG when set |
| `AZURE_SEARCH_INDEX` | `calorch-knowledge` (default) | Target index |
| `AZURE_SEARCH_API_KEY` | Key Vault reference | Query+index key; omit to use managed identity (grant the app **Search Index Data Contributor**) |
| `AZURE_SEARCH_SEMANTIC_CONFIG` | name of a semantic config | Enables semantic ranking when set |
| `RAG_TOP_K` | `4` (default) | Passages injected per enrichment call |
| `KNOWLEDGE_WRITEBACK` | `true` (default) | Index each prepared analysis (the write side of the loop) |

### 5.7 Data providers (all optional — degrade gracefully)

| Setting | Purpose |
|---|---|
| `SEC_USER_AGENT` | **Set a real contact** (`"Your Name you@example.com"`) — SEC EDGAR requires it |
| `SEC_WATCHLIST` | CSV of tickers for ingestion (default 10 mega-caps) |
| `TIINGO_API_KEY`, `FRED_API_KEY` | Prices/consensus, macro |
| `USE_FRED`, `USE_FED_H15`, `USE_IXBRL_SEGMENTS`, `USE_SEC_EFTS` | Feature flags, all default `true` |
| `FACTSET_API_KEY`, `BLOOMBERG_BLPAPI_HOST`, `LSEG_CLIENT_ID`, `SPGLOBAL_API_KEY` | Licensed providers (stubs until wired) |

### 5.8 Extensibility & observability

| Setting | Purpose |
|---|---|
| `CALORCH_AGENT_MODULES` | Comma-separated import paths of out-of-tree agent modules to register at startup |
| `LANGSMITH_API_KEY`, `LANGSMITH_PROJECT`, `LANGSMITH_TRACING` | Optional LangSmith tracing of agent subgraphs |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | Set automatically when the app is created with `--app-insights` |

One command, all at once (adjust to taste):

```bash
az functionapp config appsettings set -g $RG -n $APP --settings \
  CRON_SCHEDULE="0 0 9 * * 1" \
  OUTPUT_DIR="/tmp/calorch-out" \
  SEC_CACHE_DIR="/tmp/calorch-cache/sec" \
  USE_MOCKS="false" \
  AZURE_STORAGE_ACCOUNT_URL="https://$STG.blob.core.windows.net" \
  USE_BLOB_PROVIDERS="true" \
  AZURE_OPENAI_ENDPOINT="https://<your-aoai>.openai.azure.com/" \
  AZURE_OPENAI_API_KEY="@Microsoft.KeyVault(VaultName=$KV;SecretName=azure-openai-api-key)" \
  AZURE_OPENAI_DEPLOYMENT="gpt-4o" \
  GRAPH_TENANT_ID="<tenant>" \
  GRAPH_CLIENT_ID="<client>" \
  GRAPH_CLIENT_SECRET="@Microsoft.KeyVault(VaultName=$KV;SecretName=graph-client-secret)" \
  GRAPH_USER_ID="research@contoso.com" \
  ONEDRIVE_DRIVE_ID="b!..." \
  REPO_BACKEND="table" \
  AZURE_SEARCH_ENDPOINT="https://<svc>.search.windows.net" \
  AZURE_SEARCH_API_KEY="@Microsoft.KeyVault(VaultName=$KV;SecretName=azure-search-key)" \
  SEC_USER_AGENT="Your Name you@example.com"
```

---

## 6. Microsoft Graph app registration (non-mock runs)

1. **Entra ID → App registrations → New registration** (single tenant).
2. **Certificates & secrets** → new client secret → store in Key Vault.
3. **API permissions → Microsoft Graph → Application permissions**:
   - `Calendars.ReadWrite` — scan events, patch event bodies with links
   - `Mail.ReadWrite` + `Mail.Send` — create drafts / send research emails
   - `Files.ReadWrite.All` — OneDrive DOCX upload
4. **Grant admin consent** for the tenant.
5. Restrict the app to the research mailbox with an
   [application access policy](https://learn.microsoft.com/graph/auth-limit-mailbox-access)
   (`New-ApplicationAccessPolicy`) — application permissions are
   tenant-wide by default, which you almost certainly don't want.

---

## 7. Deploy the code

From the repository root:

```bash
func azure functionapp publish $APP --python
```

This performs a remote build (Oryx) — dependencies in `pyproject.toml`
are installed server-side, so your local platform doesn't matter.
`function_app.py`, `host.json`, and `src/calorch/**` are packaged
automatically.

Verify all 13 functions registered:

```bash
az functionapp function list -g $RG -n $APP --query "[].name" -o tsv
# expected:
# calorch_orchestrator, timer_start, http_start, http_approval, http_status,
# http_review, http_decision,
# activity_scan_calendar, activity_classify, activity_agent,
# activity_deliver, activity_aggregate_briefing, activity_request_approval
```

---

## 8. Smoke test

HTTP triggers use the **function** auth level, so requests need a key:

```bash
FUNC_KEY=$(az functionapp keys list -g $RG -n $APP --query functionKeys.default -o tsv)
BASE=https://$APP.azurewebsites.net/api

# 1. start a run (draft mode — no emails sent)
curl -sS -X POST "$BASE/run?code=$FUNC_KEY" \
  -H "Content-Type: application/json" \
  -d '{"start":"2026-06-08T00:00:00Z","end":"2026-06-15T00:00:00Z","send_emails":false}'
# → 202 with statusQueryGetUri etc.; note the run id (instance id)

# 2. poll status
curl -sS "$BASE/status/<instance_id>?code=$FUNC_KEY" | python -m json.tool
# runtime_status: Running → Completed; body exposes counts
# (event_count, approval_status, error_count, followup_count)

# 3. approval-gated send run (with APPROVER_EMAILS configured)
curl -sS -X POST "$BASE/run?code=$FUNC_KEY" \
  -d '{"send_emails":true,"require_approval":true,"approval_timeout_hours":24}'
# Each approver receives an email with a tokenized review-page link:
#   GET $BASE/review/<instance_id>?token=…   (renders the email previews)
# Open it in a browser and click Approve & send (a POST form — the emailed
# link itself is read-only). API/automation alternative, with the key:
curl -sS -X POST "$BASE/approval/<instance_id>?code=$FUNC_KEY" \
  -d '{"approved":true}'
```

While `USE_MOCKS=true`, step 1 completes against seed data with no
external credentials — do that first on a fresh environment.

Check artifacts landed in blob storage:

```bash
az storage blob list --account-name $STG -c calorch-outputs --auth-mode login \
  --query "[].name" -o tsv | head
```

---

## 9. Data ingestion (separate pipeline)

The orchestrator reads pre-ingested provider data from
`calorch-inputs` (`USE_BLOB_PROVIDERS=true`); the ingestion pipeline
(`calorch.data_ingestion.run_daily_ingestion`) fetches SEC/FRED/Tiingo
data and writes it there. It is intentionally **not** wired into the
weekly orchestrator. Options:

- **Add a timer function** (recommended): a small blueprint calling
  `run_daily_ingestion()` on a nightly schedule — e.g. `0 30 22 * * 1-5`
  (after US market close).
- **Run it ad hoc** from any machine with the same env vars:
  `python -c "from calorch.data_ingestion import run_daily_ingestion; run_daily_ingestion()"`

If `calorch-inputs` is empty, enrichment degrades to live API calls or
"—" placeholders — runs still complete.

---

## 10. Monitoring & operations

**Application Insights** is wired automatically. Useful KQL:

```kusto
// orchestration outcomes
traces
| where message startswith "Orchestration" or customDimensions.Category == "Host.Triggers.DurableTask"
| order by timestamp desc

// activity failures (these are retried 3x by RetryOptions)
exceptions
| where cloud_RoleName == "func-calorch-prod"
| summarize count() by type, outerMessage, bin(timestamp, 1h)
```

**Durable instance management** without writing code:

```bash
# list/inspect/terminate via the CLI
func durable get-instances     --connection-string-setting AzureWebJobsStorage --task-hub-name CalorchTaskHub
func durable get-history --id <instance_id> ...
func durable terminate   --id <instance_id> --reason "stuck" ...
```

**Operational facts worth knowing:**

- The approval gate races an external event against a durable timer
  (default 24 h, per-run override `approval_timeout_hours`). While
  paused, the app scales to zero — no compute cost.
- Replays are deterministic: the orchestrator derives `run_id` from
  `context.current_utc_datetime`, never the wall clock.
- Delivery is idempotent per `run_id:event_id` (recorded in the
  repository), so activity retries can't double-send an email.
- `local.settings.json` ships `AzureWebJobs.<Function>.Disabled` flags —
  flip `AzureWebJobs.timer_start.Disabled=true` in app settings to pause
  scheduled runs without redeploying.

---

## 11. CI/CD (GitHub Actions)

```yaml
# .github/workflows/deploy.yml
name: deploy
on:
  push:
    branches: [main]
jobs:
  deploy:
    runs-on: ubuntu-latest
    permissions:
      id-token: write     # OIDC federated credential — no publish profile secrets
      contents: read
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.11" }
      - run: pip install -e . && python -m pytest tests -q
      - uses: azure/login@v2
        with:
          client-id: ${{ secrets.AZURE_CLIENT_ID }}
          tenant-id: ${{ secrets.AZURE_TENANT_ID }}
          subscription-id: ${{ secrets.AZURE_SUBSCRIPTION_ID }}
      - uses: Azure/functions-action@v1
        with:
          app-name: func-calorch-prod
          remote-build: true
```

Configure the federated credential on an Entra app
(`azure/login` OIDC) with the Website Contributor role on the function
app — no secrets in the repo.

---

## 12. Security checklist

- [ ] All secrets are Key Vault references; nothing sensitive in plain app settings
- [ ] Managed identity used for blob access (`AZURE_STORAGE_ACCOUNT_URL`, no connection string)
- [ ] Graph app restricted to the research mailbox via application access policy
- [ ] Function keys rotated on a schedule (`az functionapp keys set`); approvers don't need keys — they use the emailed review link
- [ ] Note: `review`/`decision` endpoints are deliberately **anonymous + per-run token** (SHA-256 verified against the orchestration's `custom_status`). No function key goes into approval emails, the emailed link is a read-only GET (mail scanners like Outlook SafeLinks prefetch GETs and must not be able to approve), and decisions are POST-only forms
- [ ] `--allow-blob-public-access false` on the storage account (set in §3)
- [ ] `SEC_USER_AGENT` carries a real contact (SEC ToS)
- [ ] Optional hardening: identity-based `AzureWebJobsStorage__accountName` instead of the bootstrap connection string; Private Endpoints + VNet (requires EP1/Flex with networking)

---

## 13. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `func azure functionapp publish` succeeds but 0 functions listed | Remote build failed late — `az functionapp log deployment show -g $RG -n $APP`; confirm Python version is 3.11 (the `azure-functions<2.0` pin does not install on 3.13) |
| Run fails immediately with `Read-only file system: './out'` | Set `OUTPUT_DIR`, `SEC_CACHE_DIR`, `AUDIT_LOG_PATH` under `/tmp` (§5.1) |
| Orchestration stuck in `Pending` | Task hub/storage mismatch — verify `AzureWebJobsStorage` and that the `CalorchTaskHub*` tables/queues exist in the storage account |
| Approval POST returns 202 but the run stays `Running` | Wrong instance id, or the gate wasn't reached yet (check `/api/status` output for `prepared_emails`) |
| Timer never fires | `CRON_SCHEDULE` is NCRONTAB **with seconds** (6 fields); also check `AzureWebJobs.timer_start.Disabled` |
| 401 on HTTP endpoints | Missing/wrong `?code=` function key |
| Weekly briefing reports 0 events | Calendar window empty, or Graph permissions/consent missing — check `activity_scan_calendar` traces |
| LLM classification falls back to keywords | `AZURE_OPENAI_*` not set or Key Vault reference unresolved (check the setting shows a green check in the portal) |

---

## 14. Cost expectations (weekly run, Flex Consumption)

| Component | Idle | Per weekly run |
|---|---|---|
| Function App (Flex) | $0 | pennies (execution time across ~10–200 activity invocations) |
| Storage (task hub + artifacts) | <$1/mo | negligible |
| Application Insights | ~free at this volume (sampling on) | — |
| Azure Table Storage (idempotency) | included in storage | negligible |
| Azure AI Search (optional RAG) | $0 Free / ~$75 Basic | flat (only if enabled) |
| Azure OpenAI | $0 | dominated by token usage: ~1 classify + ~6 enrichment calls per event |

The approval pause costs nothing — state waits in the task hub, not on
a running instance.
