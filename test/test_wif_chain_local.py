"""Level 1 smoke test: prove the whole GCP trust chain from your laptop.

No Azure deployment needed. The script:
  1. Obtains an Entra ID JWT (from $SUBJECT_TOKEN, or via `az account
     get-access-token --resource <BROKER_AUDIENCE>`)
  2. Decodes it and prints iss / sub / aud / remaining lifetime
  3. Exchanges it at Google STS for a federated token
  4. Impersonates the keyless service account (generateAccessToken)
  5. Runs a query against a BigQuery PUBLIC dataset and prints the rows

Because the query hits `bigquery-public-data`, no customer data is involved;
only a billing project is needed.

See test/README.md for the one-time setup (the provider must trust the
subject you test with).

Usage:
    export GCP_PROJECT_ID=my-gcp-project
    export GCP_PROJECT_NUMBER=123456789012
    export GCP_SERVICE_ACCOUNT=bq-powerbi-broker@my-gcp-project.iam.gserviceaccount.com
    # optional: WIF_POOL_ID, WIF_PROVIDER_ID, BROKER_AUDIENCE, BQ_BILLING_PROJECT, SUBJECT_TOKEN
    python test_wif_chain_local.py
"""

import base64
import json
import os
import subprocess
import sys
import time

import requests

PROJECT_ID = os.environ["GCP_PROJECT_ID"]
PROJECT_NUMBER = os.environ["GCP_PROJECT_NUMBER"]
SA_EMAIL = os.environ["GCP_SERVICE_ACCOUNT"]
POOL_ID = os.environ.get("WIF_POOL_ID", "powerbi-broker-pool")
PROVIDER_ID = os.environ.get("WIF_PROVIDER_ID", "entra")
AUDIENCE = os.environ.get("BROKER_AUDIENCE", "api://powerbi-bq-broker")
BILLING_PROJECT = os.environ.get("BQ_BILLING_PROJECT", PROJECT_ID)

QUERY = (
    "SELECT name, SUM(number) AS total "
    "FROM `bigquery-public-data.usa_names.usa_1910_2013` "
    "WHERE state = 'FL' GROUP BY name ORDER BY total DESC LIMIT 5"
)


def decode_jwt(token: str) -> dict:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def get_subject_token() -> str:
    token = os.environ.get("SUBJECT_TOKEN")
    if token:
        print("[1] Using SUBJECT_TOKEN from the environment")
        return token
    print(f"[1] Requesting an Entra token via az CLI (resource {AUDIENCE})")
    out = subprocess.run(
        ["az", "account", "get-access-token", "--resource", AUDIENCE, "-o", "json"],
        capture_output=True, text=True, check=True,
    )
    return json.loads(out.stdout)["accessToken"]


def main() -> int:
    subject_token = get_subject_token()
    claims = decode_jwt(subject_token)
    ttl = int(claims["exp"] - time.time())
    print(f"    iss = {claims.get('iss')}")
    print(f"    sub = {claims.get('sub')}  <- the provider must trust this subject")
    print(f"    aud = {claims.get('aud')}")
    print(f"    expires in {ttl // 60} min (short-lived: OK)" if ttl < 5400 else "    WARNING: token unusually long-lived")

    print("[2] Exchanging at Google STS (Workload Identity Federation)")
    sts = requests.post(
        "https://sts.googleapis.com/v1/token",
        json={
            "grantType": "urn:ietf:params:oauth:grant-type:token-exchange",
            "audience": (
                f"//iam.googleapis.com/projects/{PROJECT_NUMBER}/locations/global/"
                f"workloadIdentityPools/{POOL_ID}/providers/{PROVIDER_ID}"
            ),
            "scope": "https://www.googleapis.com/auth/cloud-platform",
            "requestedTokenType": "urn:ietf:params:oauth:token-type:access_token",
            "subjectToken": subject_token,
            "subjectTokenType": "urn:ietf:params:oauth:token-type:jwt",
        },
        timeout=30,
    )
    if not sts.ok:
        print(f"    STS refused: {sts.status_code} {sts.text}")
        print("    Check issuer, allowed-audiences and attribute-condition on the provider.")
        return 1
    federated = sts.json()
    print(f"    OK, federated token, expires_in = {federated.get('expires_in')}s")

    print(f"[3] Impersonating {SA_EMAIL} (generateAccessToken)")
    imp = requests.post(
        "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/"
        f"{SA_EMAIL}:generateAccessToken",
        headers={"Authorization": f"Bearer {federated['access_token']}"},
        json={"scope": ["https://www.googleapis.com/auth/bigquery.readonly"]},
        timeout=30,
    )
    if not imp.ok:
        print(f"    Impersonation refused: {imp.status_code} {imp.text}")
        print("    Check the roles/iam.workloadIdentityUser binding for your subject.")
        return 1
    sa_token = imp.json()["accessToken"]
    print(f"    OK, SA token expires at {imp.json().get('expireTime')}")

    print(f"[4] Querying BigQuery public dataset (billing project {BILLING_PROJECT})")
    bq = requests.post(
        f"https://bigquery.googleapis.com/bigquery/v2/projects/{BILLING_PROJECT}/queries",
        headers={"Authorization": f"Bearer {sa_token}"},
        json={"query": QUERY, "useLegacySql": False, "timeoutMs": 60000},
        timeout=120,
    )
    if not bq.ok:
        print(f"    BigQuery refused: {bq.status_code} {bq.text}")
        print("    Check roles/bigquery.jobUser on the billing project.")
        return 1
    body = bq.json()
    fields = [f["name"] for f in body["schema"]["fields"]]
    print(f"    OK. Top names in FL since 1910 ({', '.join(fields)}):")
    for row in body.get("rows", []):
        print("      " + "  ".join(cell.get("v", "") for cell in row["f"]))

    print("\nPASS: full chain worked with only short-lived tokens. No key was used.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
