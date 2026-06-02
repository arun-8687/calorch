<#
.SYNOPSIS
  One-shot Azure deploy of calorch onto Container Apps (Consumption).

.DESCRIPTION
  Creates or reuses:
    - Resource group
    - Azure Container Registry (Basic, ~$5/mo)
    - Log Analytics workspace
    - Container Apps Environment
    - Container App (scale 0..3, weekly cron trigger)
    - Cosmos DB account (Serverless, ~$0.25/GB consumed)
    - User-assigned managed identity with AcrPull role

  Total idle cost: ~$5/mo (ACR) + pennies for Cosmos serverless. A weekly
  5-minute run adds <$1/mo.

.PARAMETER ResourceGroup
  Default: rg-calorch-prod

.PARAMETER Location
  Default: eastus

.PARAMETER AcrName
  Globally unique, 5-50 chars, alphanumeric. Default: acrcalorch$RANDOM

.PARAMETER ImageTag
  Default: latest

.EXAMPLE
  .\deploy\deploy.ps1
#>
[CmdletBinding()]
param(
    [string]$ResourceGroup = "rg-calorch-prod",
    [string]$Location = "eastus",
    [string]$AcrName = ("acrcalorch" + (Get-Random -Maximum 9999)),
    [string]$ImageTag = "latest",
    [string]$CosmosAccount = ("cosmos-calorch-" + (Get-Random -Maximum 9999)),
    [hashtable]$EnvOverrides = @{}
)

$ErrorActionPreference = "Stop"

function Value($name, $default = "") {
    if ($EnvOverrides.ContainsKey($name) -and $null -ne $EnvOverrides[$name]) {
        return [string]$EnvOverrides[$name]
    }
    $value = [Environment]::GetEnvironmentVariable($name)
    return $value ? $value : $default
}

function Require($name) {
    if (-not (Value $name)) {
        Write-Error "Missing required env var: $name"
    }
}

# ---------------------------------------------------------------------------
# 1. Preflight — required secrets
# ---------------------------------------------------------------------------
Write-Host "[1/8] Checking prerequisites..." -ForegroundColor Cyan
foreach ($v in @("AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT",
                "GRAPH_TENANT_ID", "GRAPH_CLIENT_ID", "GRAPH_CLIENT_SECRET",
                "GRAPH_USER_ID", "ONEDRIVE_DRIVE_ID", "SEC_USER_AGENT",
                "CHECKPOINT_POSTGRES_URI", "CALORCH_API_KEY")) {
    Require $v
}

# ---------------------------------------------------------------------------
# 2. Login + subscription
# ---------------------------------------------------------------------------
Write-Host "[2/8] Verifying az login..." -ForegroundColor Cyan
$ctx = az account show --output json 2>$null | ConvertFrom-Json
if (-not $ctx) {
    az login
    $ctx = az account show --output json | ConvertFrom-Json
}
$SubscriptionId = $ctx.id
Write-Host "       subscription: $($ctx.name) ($SubscriptionId)"

# ---------------------------------------------------------------------------
# 3. Resource group
# ---------------------------------------------------------------------------
Write-Host "[3/8] Creating resource group $ResourceGroup in $Location..." -ForegroundColor Cyan
az group create --name $ResourceGroup --location $Location --output none

# ---------------------------------------------------------------------------
# 4. Azure Container Registry
# ---------------------------------------------------------------------------
Write-Host "[4/8] Creating ACR $AcrName..." -ForegroundColor Cyan
az acr create `
    --resource-group $ResourceGroup `
    --name $AcrName `
    --sku Basic `
    --admin-enabled false `
    --output none
$ACR_ID = az acr show --name $AcrName --resource-group $ResourceGroup --query id -o tsv

# ---------------------------------------------------------------------------
# 5. Cosmos DB (serverless)
# ---------------------------------------------------------------------------
Write-Host "[5/8] Creating Cosmos DB $CosmosAccount..." -ForegroundColor Cyan
az cosmosdb create `
    --resource-group $ResourceGroup `
    --name $CosmosAccount `
    --capabilities EnableServerless `
    --locations regionName=$Location failoverPriority=0 `
    --output none
$COSMOS_ENDPOINT = "https://$CosmosAccount.sql.cosmos.azure.com:443/"
$COSMOS_KEY = az cosmosdb keys list --name $CosmosAccount --resource-group $ResourceGroup --query primaryMasterKey -o tsv
az cosmosdb sql database create --account-name $CosmosAccount --resource-group $ResourceGroup --name calorch --output none
az cosmosdb sql container create `
    --account-name $CosmosAccount --resource-group $ResourceGroup `
    --database-name calorch --name events --partition-key-path "/event_id" `
    --throughput 400 --output none

# ---------------------------------------------------------------------------
# 6. Container Apps environment + Log Analytics
# ---------------------------------------------------------------------------
Write-Host "[6/8] Creating Container Apps environment..." -ForegroundColor Cyan
$LAW = "law-calorch-$Location"
az monitor log-analytics workspace create `
    --resource-group $ResourceGroup --workspace-name $LAW --output none
$LAW_ID = az monitor log-analytics workspace show --resource-group $ResourceGroup --workspace-name $LAW --query customerId -o tsv
$LAW_KEY = az monitor log-analytics workspace get-shared-keys --resource-group $ResourceGroup --workspace-name $LAW --query primarySharedKey -o tsv

$ACA_ENV = "cae-calorch-prod"
az containerapp env create `
    --name $ACA_ENV --resource-group $ResourceGroup --location $Location `
    --logs-workspace-id $LAW_ID --logs-workspace-key $LAW_KEY `
    --output none

# ---------------------------------------------------------------------------
# 7. Build + push image
# ---------------------------------------------------------------------------
Write-Host "[7/8] Building and pushing image to ACR..." -ForegroundColor Cyan
az acr login --name $AcrName
az acr build `
    --registry $AcrName `
    --image "calorch:$ImageTag" `
    --file Dockerfile `
    .

# ---------------------------------------------------------------------------
# 8. Deploy Container App with envsubst
# ---------------------------------------------------------------------------
Write-Host "[8/8] Deploying Container App..." -ForegroundColor Cyan

$envMap = @{
    AZ_SUBSCRIPTION_ID      = $SubscriptionId
    AZ_RESOURCE_GROUP       = $ResourceGroup
    AZ_LOCATION             = $Location
    ACR_NAME                = $AcrName
    COSMOS_ENDPOINT         = $COSMOS_ENDPOINT
    COSMOS_KEY              = $COSMOS_KEY
    AZURE_OPENAI_API_KEY    = (Value "AZURE_OPENAI_API_KEY")
    AZURE_OPENAI_ENDPOINT   = (Value "AZURE_OPENAI_ENDPOINT")
    AZURE_OPENAI_DEPLOYMENT = (Value "AZURE_OPENAI_DEPLOYMENT" "gpt-4o")
    GRAPH_TENANT_ID         = (Value "GRAPH_TENANT_ID")
    GRAPH_CLIENT_ID         = (Value "GRAPH_CLIENT_ID")
    GRAPH_CLIENT_SECRET     = (Value "GRAPH_CLIENT_SECRET")
    GRAPH_USER_ID           = (Value "GRAPH_USER_ID")
    ONEDRIVE_DRIVE_ID       = (Value "ONEDRIVE_DRIVE_ID")
    SEC_USER_AGENT          = (Value "SEC_USER_AGENT")
    LANGSMITH_API_KEY       = (Value "LANGSMITH_API_KEY")
    CHECKPOINT_POSTGRES_URI = (Value "CHECKPOINT_POSTGRES_URI")
    CALORCH_API_KEY         = (Value "CALORCH_API_KEY")
}

# Use envsubst to fill the YAML template
foreach ($k in $envMap.Keys) {
    [Environment]::SetEnvironmentVariable($k, [string]$envMap[$k], "Process")
}
$yaml = Get-Content deploy/containerapp.yaml -Raw
foreach ($k in $envMap.Keys) {
    $placeholder = '${' + $k + '}'
    $yaml = $yaml.Replace($placeholder, $envMap[$k])
}
$tmp = New-TemporaryFile
Set-Content -Path $tmp -Value $yaml

az containerapp create `
    --name calorch `
    --resource-group $ResourceGroup `
    --yaml $tmp `
    --output none

# Pull role assignment: grant the managed identity AcrPull on ACR
$PRINCIPAL = az containerapp show --name calorch --resource-group $ResourceGroup --query identity.principalId -o tsv
az role assignment create --assignee $PRINCIPAL --role AcrPull --scope $ACR_ID --output none

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
$FQDN = az containerapp show --name calorch --resource-group $ResourceGroup --query properties.configuration.ingress.fqdn -o tsv
Write-Host ""
Write-Host "=========================================" -ForegroundColor Green
Write-Host "calorch deployed!" -ForegroundColor Green
Write-Host "  URL:       https://$FQDN" -ForegroundColor Green
Write-Host "  Health:    https://$FQDN/health" -ForegroundColor Green
Write-Host "  Trigger:   POST https://$FQDN/run" -ForegroundColor Green
Write-Host "  ACR:       $AcrName.azurecr.io/calorch:$ImageTag" -ForegroundColor Green
Write-Host "  Cosmos:    $CosmosAccount (serverless)" -ForegroundColor Green
Write-Host "  RG:        $ResourceGroup" -ForegroundColor Green
Write-Host "=========================================" -ForegroundColor Green
Write-Host "Estimated idle cost: ~$5/mo (ACR Basic + Cosmos serverless storage)."
Write-Host "Estimated run cost:  ~$1/mo (weekly 5-min Container Apps invocation)."
