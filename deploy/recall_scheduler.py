"""Upsert the minute recovery drain without putting its secret in CLI arguments.

Run with the project venv after deploying Recall and provisioning its secrets.
"""
import argparse
import subprocess

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default="gastrobrain-production")
    parser.add_argument("--region", default="asia-northeast1")
    parser.add_argument("--api-url", required=True)
    args = parser.parse_args()
    if not args.api_url.startswith("https://"):
        parser.error("--api-url must use HTTPS")
    token = subprocess.check_output(["gcloud", "auth", "print-access-token"], text=True).strip()
    secret = subprocess.check_output(["gcloud", "secrets", "versions", "access", "latest",
        "--secret=RECALL_WORKER_TOKEN", f"--project={args.project}"], text=True).strip()
    parent = f"projects/{args.project}/locations/{args.region}"
    name = f"{parent}/jobs/gastrobrain-recall-drain"
    job = {"name": name, "schedule": "* * * * *", "timeZone": "Etc/UTC", "attemptDeadline": "180s",
           "httpTarget": {"uri": args.api_url.rstrip("/") + "/v1/recall/drain", "httpMethod": "POST",
                          "headers": {"Authorization": "Bearer " + secret}}}
    with httpx.Client(base_url="https://cloudscheduler.googleapis.com/v1/", timeout=30,
                      headers={"Authorization": "Bearer " + token}) as client:
        found = client.get(name)
        if found.status_code == 404:
            response = client.post(f"{parent}/jobs", json=job)
        elif found.is_success:
            response = client.patch(name, params={"updateMask": "schedule,timeZone,attemptDeadline,httpTarget"}, json=job)
        else:
            raise SystemExit(f"Scheduler lookup failed: HTTP {found.status_code}")
        if not response.is_success:
            raise SystemExit(f"Scheduler update failed: HTTP {response.status_code}")
    print("Recall minute drain configured")


if __name__ == "__main__":
    main()
