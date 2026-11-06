"""Sample Azure Function: Power BI to BigQuery WIF broker.

Receives query requests from Power BI, exchanges its Managed Identity token
for short-lived Google credentials via Workload Identity Federation, runs an
allowlisted BigQuery query, and returns rows as JSON.

The token chain is spelled out explicitly (STS exchange + impersonation) for
teaching purposes. In production, prefer the google-auth external_account
credential type, which does the same thing with less code.

Required app settings (no secret among them):
    ENTRA_TENANT_ID         Entra tenant ID
    BROKER_AUDIENCE         Application ID URI, e.g. api://powerbi-bq-broker
    GCP_PROJECT_ID          GCP project ID (string)
    GCP_PROJECT_NUMBER      GCP project number (numeric)
    WIF_POOL_ID             Workload identity pool, e.g. powerbi-broker-pool
    WIF_PROVIDER_ID         Provider in the pool, e.g. entra
    GCP_SERVICE_ACCOUNT     e.g. bq-powerbi-broker@<project>.iam.gserviceaccount.com
    BQ_BILLING_PROJECT      Project that pays for the query (defaults to GCP_PROJECT_ID)

Protect this Function with Entra ID (Easy Auth) so only authorized Power BI
callers reach it. The Function itself holds no credential of any kind.
"""

import json
import logging
import os
import pathlib
import time

import azure.functions as func
import requests
from azure.identity import ManagedIdentityCredential

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)  # Easy Auth in front

BROKER_AUDIENCE = os.environ.get("BROKER_AUDIENCE", "api://powerbi-bq-broker")
GCP_PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "")
GCP_PROJECT_NUMBER = os.environ.get("GCP_PROJECT_NUMBER", "")
WIF_POOL_ID = os.environ.get("WIF_POOL_ID", "powerbi-broker-pool")
WIF_PROVIDER_ID = os.environ.get("WIF_PROVIDER_ID", "entra")
GCP_SERVICE_ACCOUNT = os.environ.get("GCP_SERVICE_ACCOUNT", "")
BQ_BILLING_PROJECT = os.environ.get("BQ_BILLING_PROJECT", GCP_PROJECT_ID)

QUERIES_PATH = pathlib.Path(__file__).parent / "queries.json"
ALLOWED_QUERIES: dict = json.loads(QUERIES_PATH.read_text(encoding="utf-8"))

# Google access token cache: reuse until ~5 minutes before expiry.
_cache = {"token": None, "exp": 0.0}


def _entra_token() -> str:
    """OIDC token for the Function's Managed Identity, audience = broker app."""
    credential = ManagedIdentityCredential()
    return credential.get_token(f"{BROKER_AUDIENCE}/.default").token


def _google_token() -> str:
    """Entra MI token -> Google STS federated token -> SA access token."""
    now = time.time()
    if _cache["token"] and now < _cache["exp"] - 300:
        return _cache["token"]

    subject_token = _entra_token()

    sts = requests.post(
        "https://sts.googleapis.com/v1/token",
        json={
            "grantType": "urn:ietf:params:oauth:grant-type:token-exchange",
            "audience": (
                f"//iam.googleapis.com/projects/{GCP_PROJECT_NUMBER}"
                f"/locations/global/workloadIdentityPools/{WIF_POOL_ID}"
                f"/providers/{WIF_PROVIDER_ID}"
            ),
            "scope": "https://www.googleapis.com/auth/cloud-platform",
            "requestedTokenType": "urn:ietf:params:oauth:token-type:access_token",
            "subjectToken": subject_token,
            "subjectTokenType": "urn:ietf:params:oauth:token-type:jwt",
        },
        timeout=30,
    )
    sts.raise_for_status()
    federated = sts.json()["access_token"]

    imp = requests.post(
        "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/"
        f"{GCP_SERVICE_ACCOUNT}:generateAccessToken",
        headers={"Authorization": f"Bearer {federated}"},
        json={"scope": ["https://www.googleapis.com/auth/bigquery.readonly"]},
        timeout=30,
    )
    imp.raise_for_status()
    _cache["token"] = imp.json()["accessToken"]
    _cache["exp"] = now + 3600
    return _cache["token"]


def _rows_as_records(result: dict) -> list:
    """Convert BigQuery jobs.query response into a list of flat dicts."""
    fields = [f["name"] for f in result.get("schema", {}).get("fields", [])]
    records = []
    for row in result.get("rows", []):
        values = [cell.get("v") for cell in row.get("f", [])]
        records.append(dict(zip(fields, values)))
    return records


@app.route(route="query/{name}", methods=["GET", "POST"])
def run_query(req: func.HttpRequest) -> func.HttpResponse:
    name = req.route_params.get("name", "")
    sql = ALLOWED_QUERIES.get(name)
    if not sql:
        return func.HttpResponse(
            json.dumps({"error": f"unknown query '{name}'", "available": sorted(ALLOWED_QUERIES)}),
            status_code=404,
            mimetype="application/json",
        )
    try:
        token = _google_token()
        resp = requests.post(
            f"https://bigquery.googleapis.com/bigquery/v2/projects/{BQ_BILLING_PROJECT}/queries",
            headers={"Authorization": f"Bearer {token}"},
            json={"query": sql, "useLegacySql": False, "timeoutMs": 60000},
            timeout=120,
        )
        resp.raise_for_status()
        body = resp.json()
        return func.HttpResponse(
            json.dumps({"query": name, "rows": _rows_as_records(body), "jobComplete": body.get("jobComplete")}),
            status_code=200,
            mimetype="application/json",
        )
    except requests.HTTPError as err:
        logging.error("Upstream error for query '%s': %s", name, err.response.text)
        return func.HttpResponse(
            json.dumps({"error": "upstream", "detail": err.response.json().get("error", {}).get("message", "")}),
            status_code=err.response.status_code,
            mimetype="application/json",
        )


@app.route(route="healthz", methods=["GET"])
def healthz(req: func.HttpRequest) -> func.HttpResponse:
    return func.HttpResponse(json.dumps({"ok": True}), mimetype="application/json")
