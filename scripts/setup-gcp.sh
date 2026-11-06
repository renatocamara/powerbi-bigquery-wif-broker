#!/usr/bin/env bash
# GCP side of the Power BI -> BigQuery WIF broker.
# Creates the workload identity pool, the OIDC provider trusting your Entra
# tenant, and a keyless service account with least-privilege BigQuery access.
set -euo pipefail

# ----- edit these -----------------------------------------------------------
PROJECT_ID="my-gcp-project"
PROJECT_NUMBER="123456789012"
TENANT_ID="00000000-0000-0000-0000-000000000000"   # Entra tenant ID
MI_OBJECT_ID="11111111-1111-1111-1111-111111111111" # Managed Identity object ID
BROKER_AUDIENCE="api://powerbi-bq-broker"
POOL_ID="powerbi-broker-pool"
PROVIDER_ID="entra"
SA_NAME="bq-powerbi-broker"
DATASET="my_dataset"                                # dataset to grant read on
# ----------------------------------------------------------------------------

SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud config set project "${PROJECT_ID}"

echo ">> Creating workload identity pool"
gcloud iam workload-identity-pools create "${POOL_ID}" \
  --location=global \
  --display-name="Power BI broker"

echo ">> Creating OIDC provider trusting the Entra tenant, pinned to the MI"
gcloud iam workload-identity-pools providers create-oidc "${PROVIDER_ID}" \
  --workload-identity-pool="${POOL_ID}" \
  --location=global \
  --issuer-uri="https://login.microsoftonline.com/${TENANT_ID}/v2.0" \
  --allowed-audiences="${BROKER_AUDIENCE}" \
  --attribute-mapping="google.subject=assertion.sub" \
  --attribute-condition="assertion.sub == '${MI_OBJECT_ID}'"

echo ">> Creating keyless service account"
gcloud iam service-accounts create "${SA_NAME}" \
  --display-name="Power BI broker (keyless)"

echo ">> Allowing the federated identity to impersonate the service account"
gcloud iam service-accounts add-iam-policy-binding "${SA_EMAIL}" \
  --role=roles/iam.workloadIdentityUser \
  --member="principal://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL_ID}/subject/${MI_OBJECT_ID}"

echo ">> Granting BigQuery jobUser on the project"
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --role=roles/bigquery.jobUser \
  --member="serviceAccount:${SA_EMAIL}"

echo ">> Granting dataset-level read (edit DATASET above)"
bq add-iam-policy-binding \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/bigquery.dataViewer" \
  "${PROJECT_ID}:${DATASET}" || echo "   (grant dataset access manually if bq is not configured)"

echo ">> Recommended: block service account key creation for good"
echo "   gcloud resource-manager org-policies enable-enforce \\"
echo "     constraints/iam.disableServiceAccountKeyCreation --project=${PROJECT_ID}"

echo ">> Done. Verify zero keys with:"
echo "   gcloud iam service-accounts keys list --iam-account=${SA_EMAIL}"
