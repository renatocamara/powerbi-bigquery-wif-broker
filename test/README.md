# Testing

Three levels, cheapest first.

## Level 1: local smoke test (simplest, no Azure deployment)

`test_wif_chain_local.py` proves the entire GCP side in minutes: pool, provider, service-account binding, BigQuery IAM, and that every token is short-lived. It queries a BigQuery **public dataset**, so no customer data is involved.

Because there is no Managed Identity on your laptop, the test uses an Entra token for **you** (via `az login`). That means a small one-time test configuration on the provider:

1. Run `az login` on the tenant.
2. On the broker's Entra app registration, add the Azure CLI (`04b07795-8ddb-461a-bbee-02f9e1bf7b46`) as an authorized client application for the exposed scope, so `az account get-access-token --resource api://powerbi-bq-broker` works.
3. Find your user object ID: `az ad signed-in-user show --query id -o tsv`.
4. For the duration of the test, either relax the provider's `attribute-condition` to also allow your object ID, or add a second `roles/iam.workloadIdentityUser` binding with `subject/<your-object-id>`. Remove it after the test.

Then:

```bash
export GCP_PROJECT_ID=my-gcp-project
export GCP_PROJECT_NUMBER=123456789012
export GCP_SERVICE_ACCOUNT=bq-powerbi-broker@my-gcp-project.iam.gserviceaccount.com
pip install requests
python test_wif_chain_local.py
```

Expected output ends with `PASS: full chain worked with only short-lived tokens.` A failure at step 2 points at the provider config, at step 3 at the impersonation binding, at step 4 at BigQuery IAM. Each failure prints which.

Alternative without touching the app registration: obtain any JWT from the tenant whose `aud` you add to `allowed-audiences` for the test, and pass it via `SUBJECT_TOKEN=<jwt>`.

## Level 2: deployed broker (Managed Identity path)

After deploying `broker/` (see `scripts/setup-azure.sh`):

```bash
TOKEN=$(az account get-access-token --resource api://powerbi-bq-broker --query accessToken -o tsv)
curl -s -H "Authorization: Bearer $TOKEN" \
  https://<app-name>.azurewebsites.net/api/query/sample_usa_names | jq .
```

Also run the negative checks from the runbook: no token returns 401, an unknown query name returns 404, and `gcloud iam service-accounts keys list` stays empty.

## Level 3: Power BI

1. Power BI Desktop: Get Data, Web, URL = the broker endpoint, authentication = Organizational account.
2. Publish, set the dataset credential to OAuth2, schedule refresh.
3. Let it run unattended for 48 hours and confirm no credential prompts and fresh data each cycle.
