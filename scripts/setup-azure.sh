#!/usr/bin/env bash
# Azure side of the Power BI -> BigQuery WIF broker.
# Deploys a Function App with a system-assigned Managed Identity and prints
# the values the GCP side needs.
set -euo pipefail

# ----- edit these -----------------------------------------------------------
RG="rg-powerbi-bq-broker"
LOCATION="eastus2"
STORAGE="stpbibqbroker$RANDOM"
APP="func-powerbi-bq-broker"
BROKER_AUDIENCE="api://powerbi-bq-broker"
# ----------------------------------------------------------------------------

echo ">> Resource group"
az group create -n "${RG}" -l "${LOCATION}" -o none

echo ">> Storage account (required by Functions)"
az storage account create -g "${RG}" -n "${STORAGE}" -l "${LOCATION}" \
  --sku Standard_LRS -o none

echo ">> Function App (Python, consumption)"
az functionapp create -g "${RG}" -n "${APP}" \
  --storage-account "${STORAGE}" \
  --consumption-plan-location "${LOCATION}" \
  --runtime python --runtime-version 3.11 --functions-version 4 -o none

echo ">> System-assigned Managed Identity"
MI_OBJECT_ID=$(az functionapp identity assign -g "${RG}" -n "${APP}" \
  --query principalId -o tsv)

TENANT_ID=$(az account show --query tenantId -o tsv)

echo ""
echo "============================================================"
echo "Carry these values to scripts/setup-gcp.sh:"
echo "  TENANT_ID     = ${TENANT_ID}"
echo "  MI_OBJECT_ID  = ${MI_OBJECT_ID}"
echo "  AUDIENCE      = ${BROKER_AUDIENCE}"
echo "============================================================"
echo ""
echo "Next steps (manual):"
echo "  1. Register an Entra app for the broker API and set its"
echo "     Application ID URI to ${BROKER_AUDIENCE}; expose one scope."
echo "  2. Enable App Service Authentication (Easy Auth) on ${APP}"
echo "     requiring that app, so only authorized callers reach it."
echo "  3. Set the app settings from broker/local.settings.json.example:"
echo "     az functionapp config appsettings set -g ${RG} -n ${APP} --settings ..."
echo "  4. Deploy the code:  func azure functionapp publish ${APP}  (from broker/)"
