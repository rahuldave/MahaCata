"""Make a Google Cloud project for the provcat GCS system tests.

It uses the Google REST APIs through google-auth, with your Application
Default Credentials (gcloud auth application-default login). It makes the
project, links it to a billing account, and turns on the Cloud Storage API.
Run it once, then run the tests with PROVCAT_SYSTEM_GCS=<project id>.

    uv run python scripts/gcs_test_project.py --billing-from <a project with billing>
    uv run python scripts/gcs_test_project.py --billing-account billingAccounts/XXXXXX-XXXXXX-XXXXXX

The tests delete every bucket they make, so the project holds no data between
runs. GCS may keep deleted objects for a few days under its soft-delete
setting. To remove the project itself, run it with --delete <project id>.

If Google refuses a call with 403 and names a quota project, run
gcloud auth application-default set-quota-project <a project with billing>.
"""
from __future__ import annotations

import argparse
import sys
import time
import uuid

import google.auth
from google.auth.transport.requests import AuthorizedSession

CRM = "https://cloudresourcemanager.googleapis.com/v3"
BILLING = "https://cloudbilling.googleapis.com/v1"
USAGE = "https://serviceusage.googleapis.com/v1"


def session() -> AuthorizedSession:
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    return AuthorizedSession(creds)


class Failed(Exception):
    pass


def check(r):
    if r.status_code >= 400:
        raise Failed(f"{r.request.method} {r.url}: {r.status_code} {r.text[:400]}")
    return r.json() if r.text else {}


def get(s, url: str) -> dict:
    """GET with a few retries, because a poll can meet a short 5xx."""
    for attempt in range(4):
        r = s.get(url)
        if r.status_code < 500 or attempt == 3:
            return check(r)
        time.sleep(2 ** attempt)


def wait(s, name: str, base: str) -> dict:
    for _ in range(60):
        op = get(s, f"{base}/{name}")
        if op.get("done"):
            if "error" in op:
                raise Failed(f"{name}: {op['error']}")
            return op
        time.sleep(2)
    raise Failed(f"{name}: did not finish")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--project", default=f"provcat-tests-{uuid.uuid4().hex[:6]}", help="the new project id")
    ap.add_argument("--billing-from", help="copy the billing account of this project")
    ap.add_argument("--billing-account", help="billingAccounts/XXXXXX-XXXXXX-XXXXXX")
    ap.add_argument("--delete", metavar="PROJECT", help="delete this project instead")
    args = ap.parse_args()
    s = session()
    try:
        if args.delete:
            wait(s, check(s.delete(f"{CRM}/projects/{args.delete}"))["name"], CRM)
            print(f"deleted {args.delete}; Google keeps it for 30 days before it is gone")
            return
        account = args.billing_account
        if args.billing_from:
            info = get(s, f"{BILLING}/projects/{args.billing_from}/billingInfo")
            account = info.get("billingAccountName")
            if not account or not info.get("billingEnabled"):
                sys.exit(f"{args.billing_from} has no billing account turned on; pick another project")
        if not account:
            sys.exit("give --billing-from or --billing-account")
    except Failed as e:
        sys.exit(str(e))
    pid = args.project
    r = s.post(f"{CRM}/projects", json={"projectId": pid, "displayName": "provcat tests"})
    if r.status_code == 409:
        print(f"project {pid} already exists; finishing its setup")
    else:
        try:
            wait(s, check(r)["name"], CRM)
        except Failed as e:
            sys.exit(str(e))
        print(f"made project {pid}")
    try:
        check(s.put(f"{BILLING}/projects/{pid}/billingInfo", json={"billingAccountName": account}))
        print(f"linked billing account {account}")
        op = check(s.post(f"{USAGE}/projects/{pid}/services/storage.googleapis.com:enable"))
        if not op.get("done"):
            wait(s, op["name"], USAGE)
    except Failed as e:
        sys.exit(f"{e}\n\nthe project {pid} exists but is not ready. Run this again with --project {pid}"
                 f" after fixing the cause, or remove it with --delete {pid}")
    print(f"turned on the Cloud Storage API\n\nrun: PROVCAT_SYSTEM_GCS={pid} uv run pytest -m system")


if __name__ == "__main__":
    main()
