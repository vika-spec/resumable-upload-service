#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════════
#  deploy-azure.sh — Deploy Resumable Upload Service to Azure Container Apps
#
#  Prerequisites:
#    1. Azure CLI installed  →  https://docs.microsoft.com/en-us/cli/azure/install-azure-cli
#    2. Docker installed and running
#    3. Azure for Students account with credits
#
#  Usage:
#    chmod +x deploy-azure.sh
#    ./deploy-azure.sh
#
#  Estimated monthly cost (light student demo traffic):
#    Azure Container Apps  ~$0.00  (well within free grant)
#    Azure Blob Storage    ~$0.50  (per 25 GB stored + operations)
#    Azure Container Reg   ~$5.00  (Basic tier)
#    SQLite on local disk  ~$0.00  (no external DB needed for demo)
#    ─────────────────────────────
#    Total                 ~$5-6 / month  (well under your $10-15 budget)
# ═══════════════════════════════════════════════════════════════════════════════
set -euo pipefail

# ── Config — edit these ───────────────────────────────────────────────────────
APP_NAME="resumable-upload"           # must be globally unique on Azure
RESOURCE_GROUP="${APP_NAME}-rg"
LOCATION="eastus"                     # or "westeurope", "centralindia", etc.
STORAGE_ACCOUNT="${APP_NAME//-/}store" # no hyphens, max 24 chars
BLOB_CONTAINER="uploads"
ACR_NAME="${APP_NAME//-/}registry"    # Azure Container Registry name

# Cost / abuse limits (these protect your Azure credits)
QUOTA_USER_STORAGE_GB=1              # 1 GB per registered user
QUOTA_GLOBAL_GB=40                   # kill-switch: stop accepting uploads at 40 GB total
QUOTA_DAILY_UPLOADS=20               # max upload sessions per user per day
MAX_FILE_SIZE_GB=2                   # max single file size

# Admin account — change this before deploying!
ADMIN_USERNAME="admin"

# ─────────────────────────────────────────────────────────────────────────────

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║  Resumable Upload Service — Azure Deploy     ║"
echo "╚══════════════════════════════════════════════╝"
echo ""

# ── Step 1: Login ─────────────────────────────────────────────────────────────
echo "▶ Step 1/8  Logging into Azure..."
az account show &>/dev/null || az login
SUBSCRIPTION=$(az account show --query id -o tsv)
echo "  ✓ Subscription: $SUBSCRIPTION"

# ── Step 2: Resource group ────────────────────────────────────────────────────
echo "▶ Step 2/8  Creating resource group '$RESOURCE_GROUP' in $LOCATION..."
az group create --name "$RESOURCE_GROUP" --location "$LOCATION" -o none
echo "  ✓ Done"

# ── Step 3: Storage account + Blob container ──────────────────────────────────
echo "▶ Step 3/8  Creating Azure Blob Storage..."
az storage account create \
    --name "$STORAGE_ACCOUNT" \
    --resource-group "$RESOURCE_GROUP" \
    --location "$LOCATION" \
    --sku Standard_LRS \
    --kind StorageV2 \
    --access-tier Hot \
    -o none

STORAGE_CONN_STR=$(az storage account show-connection-string \
    --name "$STORAGE_ACCOUNT" \
    --resource-group "$RESOURCE_GROUP" \
    --query connectionString -o tsv)

az storage container create \
    --name "$BLOB_CONTAINER" \
    --connection-string "$STORAGE_CONN_STR" \
    -o none

echo "  ✓ Storage account: $STORAGE_ACCOUNT"
echo "  ✓ Blob container:  $BLOB_CONTAINER"

# ── Step 4: Azure Container Registry ─────────────────────────────────────────
echo "▶ Step 4/8  Creating Container Registry '$ACR_NAME'..."
az acr create \
    --name "$ACR_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --sku Basic \
    --admin-enabled true \
    -o none

ACR_SERVER=$(az acr show --name "$ACR_NAME" --query loginServer -o tsv)
ACR_PASS=$(az acr credential show --name "$ACR_NAME" --query "passwords[0].value" -o tsv)
echo "  ✓ Registry: $ACR_SERVER"

# ── Step 5: Build & push Docker image ────────────────────────────────────────
echo "▶ Step 5/8  Building and pushing Docker image (this takes 2-3 minutes)..."
az acr build \
    --registry "$ACR_NAME" \
    --image "${APP_NAME}:latest" \
    . \
    -o none
echo "  ✓ Image pushed to $ACR_SERVER/${APP_NAME}:latest"

# ── Step 6: Generate a strong secret key ──────────────────────────────────────
echo "▶ Step 6/8  Generating JWT secret key..."
SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
echo "  ✓ Secret key generated (stored only in Azure, never logged)"

# ── Step 7: Container Apps environment ────────────────────────────────────────
echo "▶ Step 7/8  Creating Container Apps environment..."
az containerapp env create \
    --name "${APP_NAME}-env" \
    --resource-group "$RESOURCE_GROUP" \
    --location "$LOCATION" \
    -o none
echo "  ✓ Environment ready"

# ── Step 8: Deploy the app ────────────────────────────────────────────────────
echo "▶ Step 8/8  Deploying Container App..."
az containerapp create \
    --name "$APP_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --environment "${APP_NAME}-env" \
    --image "${ACR_SERVER}/${APP_NAME}:latest" \
    --registry-server "$ACR_SERVER" \
    --registry-username "$ACR_NAME" \
    --registry-password "$ACR_PASS" \
    --target-port 8000 \
    --ingress external \
    --min-replicas 0 \
    --max-replicas 2 \
    --cpu 0.5 \
    --memory 1Gi \
    --env-vars \
        "SECRET_KEY=${SECRET_KEY}" \
        "AZURE_STORAGE_CONNECTION_STRING=${STORAGE_CONN_STR}" \
        "AZURE_STORAGE_CONTAINER=${BLOB_CONTAINER}" \
        "ADMIN_USERNAME=${ADMIN_USERNAME}" \
        "QUOTA_USER_STORAGE_GB=${QUOTA_USER_STORAGE_GB}" \
        "QUOTA_GLOBAL_GB=${QUOTA_GLOBAL_GB}" \
        "QUOTA_DAILY_UPLOADS=${QUOTA_DAILY_UPLOADS}" \
        "MAX_FILE_SIZE_GB=${MAX_FILE_SIZE_GB}" \
        "LOG_FORMAT=json" \
        "LOG_LEVEL=INFO" \
        "CORS_ORIGINS=*" \
    -o none

APP_URL=$(az containerapp show \
    --name "$APP_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --query properties.configuration.ingress.fqdn -o tsv)

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  ✅  DEPLOYMENT COMPLETE                                     ║"
echo "╠══════════════════════════════════════════════════════════════╣"
echo "║                                                              ║"
echo "║  App URL:      https://${APP_URL}"
echo "║  Health check: https://${APP_URL}/health"
echo "║  API docs:     https://${APP_URL}/docs"
echo "║                                                              ║"
echo "║  Admin login:  username = ${ADMIN_USERNAME}"
echo "║  (register this account first, then GET /api/admin/stats)   ║"
echo "║                                                              ║"
echo "║  Cost limits active:                                         ║"
echo "║    Per-user quota : ${QUOTA_USER_STORAGE_GB} GB                              ║"
echo "║    Global cap     : ${QUOTA_GLOBAL_GB} GB  (hard kill-switch)          ║"
echo "║    Daily sessions : ${QUOTA_DAILY_UPLOADS} per user per day                 ║"
echo "║    Max file size  : ${MAX_FILE_SIZE_GB} GB                              ║"
echo "║                                                              ║"
echo "╠══════════════════════════════════════════════════════════════╣"
echo "║  To update after code changes:                               ║"
echo "║    az acr build --registry $ACR_NAME --image ${APP_NAME}:latest ."
echo "║    az containerapp update --name $APP_NAME --resource-group $RESOURCE_GROUP --image ${ACR_SERVER}/${APP_NAME}:latest"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

# Save deployment info locally
cat > .azure-deployment.env << ENVEOF
APP_URL=https://${APP_URL}
RESOURCE_GROUP=${RESOURCE_GROUP}
APP_NAME=${APP_NAME}
ACR_NAME=${ACR_NAME}
ACR_SERVER=${ACR_SERVER}
STORAGE_ACCOUNT=${STORAGE_ACCOUNT}
ENVEOF

echo "  Deployment details saved to .azure-deployment.env"
echo "  Add .azure-deployment.env to your .gitignore!"
