# Runbook: Power BI to BigQuery via an Azure WIF Broker

This runbook stands up an Azure-hosted identity and query broker so Power BI can refresh BigQuery data with no long-lived GCP credential anywhere in the chain. The customer in this document is fictional (Contoso); replace names and IDs with your own.

Audience: the Power BI team, the Contoso GCP platform team, and the Azure team that will own the broker.

## 1. Context: the requirement

Contoso Security requires least-privilege access and no standing secrets. The native Power BI BigQuery connector authenticates with a service-account JSON key that lives about a year in plain text, so a leaked file means a year of exposure.

Options already evaluated and set aside:

| Option | Outcome | Reason |
| --- | --- | --- |
| One all-access account with a longer-lived token | Rejected | Maximum blast radius, violates least privilege |
| Third-party ODBC connector | Rejected | Anyone who knows the DSN can use it; no per-user security |
| Workforce Identity Federation with exception groups | Does not scale | Tokens do not auto-renew for scheduled refresh; would need hundreds of groups or one all-access ID |
| Custom REST WIF connector inside Power BI | Works, but limited | Short-lived tokens, yet ~10x slower, ~5x payload, no DirectQuery, no Report Builder |

The broker pattern keeps the security properties of the custom connector while moving the WIF responsibility out of Power BI and into an Azure workload, where token renewal happens automatically on every request.

## 2. Target architecture

```
Power BI Service  (scheduled refresh, OAuth2 sign-in)
      |  Entra ID OAuth2 token, renewed automatically by the Service
      v
Azure broker  (Function / App Service / AKS + Managed Identity, no retrievable secret)
      |  MI OIDC token, audience api://powerbi-bq-broker, ~1h
      v
Google STS + Workload Identity Pool  (provider pinned to the MI subject)
      |  federated token, then generateAccessToken, ~1h
      v
GCP Service Account  (zero keys by org policy)
      |  short-lived SA access token, REST query
      v
BigQuery  (dataset-level dataViewer + project jobUser)
```

Power BI talks only to the broker, with Entra ID OAuth. The broker owns the WIF exchange: it presents its Managed Identity token to Google STS and impersonates a keyless service account, so every refresh mints fresh credentials end to end and nothing persists between runs.

## 3. Why no long-lived secret exists

Every credential in the chain is minted on demand at refresh time and expires on its own. Nothing is persisted that could leak or that needs rotation.

| Link in the chain | Credential | Lifetime | Where it is stored |
| --- | --- | --- | --- |
| Power BI to broker | Entra ID OAuth2 access token | ~60 to 90 min | Nowhere; the Power BI Service acquires and renews it automatically |
| Broker identity | Azure Managed Identity | No retrievable secret exists | Platform-managed; Azure issues tokens on demand |
| Broker to Google STS | Entra OIDC token for the managed identity | ~1 hour | In memory only, requested per refresh |
| Google STS exchange | Federated STS token | ~1 hour | In memory only |
| Service-account impersonation | Short-lived SA access token | Default 1 hour | In memory only |
| GCP service account | Zero keys ever created | n/a | Key creation blocked by org policy (see Hardening) |

Two points worth making explicitly to the security review:

1. The GCP service account can be made keyless by construction, not just by convention, with the org policy `constraints/iam.disableServiceAccountKeyCreation`.
2. This answers the auto-renew objection raised against Workforce Identity Federation: renewal is server-side and per request, so scheduled refresh never depends on a human re-authenticating or on a stored credential.

## 4. Prerequisites

- Azure: a subscription and permission to deploy an Azure Function or App Service, enable a managed identity, and register an Entra ID application for the broker API. Record the tenant ID.
- GCP: `roles/iam.workloadIdentityPoolAdmin` at the organization or project level, permission to create service accounts and grant IAM on the BigQuery datasets in scope. Record the project ID and project number.
- Power BI: workspace admin rights and the ability to set data source credentials for scheduled refresh.
- Network egress from the broker to `sts.googleapis.com`, `iamcredentials.googleapis.com`, and `bigquery.googleapis.com` (add to the allowlist if the broker runs with restricted VNet egress).
- An agreed least-privilege matrix: which datasets each service account may read, signed off by the data owners before IAM grants are made.

## 5. Step 1: Deploy the Azure broker with a Managed Identity

The broker is a small API (Azure Function or App Service; AKS works the same way) that receives query requests from Power BI and performs the GCP token exchange server-side. A sample implementation is in [`broker/`](../broker/).

1. Deploy the Function App or App Service in the target subscription and region.
2. Enable a system-assigned managed identity:

   ```bash
   az functionapp identity assign -g <resource-group> -n <app-name>
   ```

3. Record the managed identity object ID (also called principal ID). The GCP side pins trust to this value.
4. Register an Entra ID application for the broker API and set its Application ID URI, for example `api://powerbi-bq-broker`. Expose one scope (for example `Query.Execute`).
5. Protect the API: either App Service Authentication (Easy Auth) requiring that app, or JWT validation in code (issuer = your tenant, audience = the Application ID URI).
6. Design the surface narrow: expose named endpoints such as `POST /api/query/<name>` with an allowlisted query set, not a raw BigQuery pass-through.

Outputs to carry forward: tenant ID, managed identity object ID, broker base URL, Application ID URI.

## 6. Step 2: GCP Workload Identity Pool and OIDC provider

This is the trust anchor: GCP accepts tokens issued by Contoso's Entra tenant for one specific managed identity, and nothing else.

```bash
gcloud iam workload-identity-pools create powerbi-broker-pool \
  --location=global \
  --display-name="Power BI broker"

gcloud iam workload-identity-pools providers create-oidc entra \
  --workload-identity-pool=powerbi-broker-pool \
  --location=global \
  --issuer-uri="https://login.microsoftonline.com/<TENANT_ID>/v2.0" \
  --allowed-audiences="api://powerbi-bq-broker" \
  --attribute-mapping="google.subject=assertion.sub" \
  --attribute-condition="assertion.sub == '<MANAGED_IDENTITY_OBJECT_ID>'"
```

Notes:

- The issuer must match the token version the managed identity returns. For v1 tokens use `https://sts.windows.net/<TENANT_ID>/`. Decode one token from the broker and copy its `iss` claim exactly.
- `allowed-audiences` must equal the audience the broker requests its Entra token for (Step 4).
- The `attribute-condition` pins the provider to the single managed identity. Any other identity in the tenant is refused at the STS, even if it obtains a token with the right audience.

## 7. Step 3: Service account and IAM bindings, with zero keys

Create the service account the broker will impersonate, bind it to the federated identity, and grant BigQuery access at dataset level. No key is ever created.

```bash
gcloud iam service-accounts create bq-powerbi-broker \
  --display-name="Power BI broker (keyless)"

gcloud iam service-accounts add-iam-policy-binding \
  bq-powerbi-broker@<PROJECT_ID>.iam.gserviceaccount.com \
  --role=roles/iam.workloadIdentityUser \
  --member="principal://iam.googleapis.com/projects/<PROJECT_NUMBER>/locations/global/workloadIdentityPools/powerbi-broker-pool/subject/<MANAGED_IDENTITY_OBJECT_ID>"
```

BigQuery grants, least privilege:

- `roles/bigquery.jobUser` on the project (run queries).
- `roles/bigquery.dataViewer` on each in-scope dataset only, never at project level.
- One service account per data domain if datasets have different owners; the broker picks the right one per endpoint.

Optional simplification: where the data owners accept it, grant the federated principal (`principal://...`) BigQuery roles directly and skip the service account entirely. That removes the impersonation hop; keep the service account model if you want a nameable principal per domain in audit logs.

## 8. Step 4: Token exchange inside the broker

Per incoming request the broker performs four calls, all with short-lived tokens held in memory only.

1. Get an Entra token for the managed identity from the identity endpoint, with `resource = api://powerbi-bq-broker` (must equal the provider's allowed audience).
2. Exchange it at Google STS, `POST https://sts.googleapis.com/v1/token`:

   ```
   grantType=urn:ietf:params:oauth:grant-type:token-exchange
   audience=//iam.googleapis.com/projects/<PROJECT_NUMBER>/locations/global/workloadIdentityPools/powerbi-broker-pool/providers/entra
   subjectTokenType=urn:ietf:params:oauth:token-type:jwt
   requestedTokenType=urn:ietf:params:oauth:token-type:access_token
   scope=https://www.googleapis.com/auth/cloud-platform
   subjectToken=<ENTRA_JWT>
   ```

3. Impersonate the service account, `POST https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/bq-powerbi-broker@<PROJECT_ID>.iam.gserviceaccount.com:generateAccessToken` with `scope = https://www.googleapis.com/auth/bigquery.readonly`, authorized by the STS token.
4. Run the query against the BigQuery REST API (`jobs.query`, then `getQueryResults` with paging) and stream rows back to the caller.

Implementation guidance:

- Prefer the Google Auth client libraries with external account (workload identity federation) credentials over hand-rolled HTTP. The credential config is a JSON file with no secret in it; it points at the Azure identity endpoint as the subject token source and the library handles steps 1 to 3. The sample in `broker/` spells the chain out explicitly for teaching purposes.
- Cache the Google access token and reuse it until about 5 minutes before expiry; do not cache across identities if more than one service account is in play.
- Return clear 403s distinguishing "caller not allowed by the broker" from "service account not granted on the dataset", which makes the validation section testable.

## 9. Step 5: Connect Power BI and enable scheduled refresh

Power BI talks only to the broker, with Entra ID OAuth. It never sees a Google credential of any kind.

1. In Power BI Desktop, connect with the Web connector (`Web.Contents` against the broker endpoints) or a thin custom connector that wraps them.
2. Authentication method: Organizational account (OAuth2) against the broker's Entra app. Do not use API keys or Basic.
3. Publish the semantic model to the Power BI Service.
4. On the dataset's settings, set the data source credential to OAuth2 and sign in once with the service identity chosen for refresh.
5. Configure scheduled refresh as usual.

Why refresh keeps working unattended: the Power BI Service renews the Entra token for the connection automatically, and the broker mints fresh Google credentials on every call. There is no token that a human must refresh and no stored key that ages out.

For large result sets, have the broker return paged JSON, or stage results as Parquet the report reads, rather than one giant response.

## 10. Validation and test plan

- [ ] A `curl` to the broker with a valid Entra token returns rows; without a token it returns 401.
- [ ] GCP Cloud Audit Logs show the caller as `principal://...workloadIdentityPools/powerbi-broker-pool/...`, proving federated auth, not a key.
- [ ] `gcloud iam service-accounts keys list` on the broker service account returns only the Google-managed entries, no user-managed keys.
- [ ] A token captured in a test decodes with `exp` no more than 1 hour out, at every hop.
- [ ] Negative test: a different managed identity in the tenant is refused at Google STS (attribute condition works).
- [ ] Negative test: the service account is denied on a dataset it was not granted (least privilege works).
- [ ] Scheduled refresh runs unattended for at least 48 hours across multiple cycles with no credential prompt.
- [ ] Refresh duration and payload size recorded and compared against the native key-based connector baseline, so the performance trade-off is quantified, not assumed.

See [`test/`](../test/) for a local smoke test that proves the GCP side before anything is deployed to Azure.

## 11. Hardening and guardrails

- Enforce the org policy `constraints/iam.disableServiceAccountKeyCreation` on the GCP project or folder, making the keyless design permanent.
- Keep the WIF provider's `attribute-condition` pinned to the managed identity object ID; review it whenever the broker is redeployed.
- Entra side: restrict the broker app to assigned users and groups, apply Conditional Access, and disable user consent for it.
- Grant BigQuery roles at dataset level only; use separate service accounts per data domain where owners differ.
- Log on both ends: GCP Cloud Audit Logs (STS exchanges and BigQuery jobs) plus App Service or Function logs into Log Analytics. Alert on STS exchanges for the pool that do not originate from the broker's egress IPs.
- Rate-limit the broker and keep the query surface allowlisted; the broker is the policy enforcement point, so a raw pass-through endpoint would undo the design.
- Consider VPC Service Controls around the BigQuery project if you want network-level containment in addition to identity-level.

## 12. Trade-offs and open items

The broker fixes credentials, not speed. It sits on the BigQuery REST path, so expect slower refresh than the native key-based connector, no DirectQuery, and no paginated Report Builder support. Position it as the secure interim, with performance quantified in the validation step.

Parallel path to re-check before investing heavily in the broker:

- The Google BigQuery (Microsoft Entra ID) connector is generally available and authenticates with Entra ID JWTs through Google's Workforce Federation, with Import and DirectQuery support.
- Its newer implementation (preview) uses the BigQuery Storage API by default, which addresses the speed and feature limits of the REST path.
- The open question for any given tenant is whether unattended scheduled refresh meets requirements with the identities involved. Validate that first; if it does, the broker becomes unnecessary.

## 13. References

- [Google BigQuery (Microsoft Entra ID) connector](https://learn.microsoft.com/en-us/power-query/connectors/google-bigquery-aad), Microsoft Learn
- [Access BigQuery data in Power BI with Workforce Identity Federation and Microsoft Entra](https://docs.cloud.google.com/iam/docs/workforce-sign-in-power-bi), Google Cloud
- [Workload Identity Federation overview](https://cloud.google.com/iam/docs/workload-identity-federation), Google Cloud
- [Configure Workload Identity Federation with Azure](https://cloud.google.com/iam/docs/workload-identity-federation-with-other-clouds), Google Cloud
- [Google STS token exchange API](https://cloud.google.com/iam/docs/reference/sts/rest/v1/TopLevel/token), Google Cloud
- [generateAccessToken (Service Account Credentials API)](https://cloud.google.com/iam/docs/reference/credentials/rest/v1/projects.serviceAccounts/generateAccessToken), Google Cloud
- [Managed identities for Azure resources](https://learn.microsoft.com/en-us/entra/identity/managed-identities-azure-resources/overview), Microsoft Learn
