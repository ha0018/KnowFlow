"""Check dependency failures against a disposable Compose project.

Run after `docker compose up`: this script deliberately stops Redis and MinIO
one at a time and always attempts to restart the stopped service.
"""

import argparse
import json
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path


def get(base_url, path):
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + path, timeout=12) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


def wait_for_ready(base_url, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if get(base_url, "/health/ready")[0] == 200:
                return
        except (OSError, ValueError):
            pass
        time.sleep(1)
    raise RuntimeError("Stack readiness did not recover")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--report", default=".runtime/dependency-faults.json")
    parser.add_argument("--project-name", default="knowflow")
    parser.add_argument("--cycles", type=int, default=1)
    args = parser.parse_args()
    if not 1 <= args.cycles <= 20:
        parser.error("cycles must be between 1 and 20")
    root = Path(__file__).resolve().parents[1]
    command = ["docker", "compose", "--project-name", args.project_name, "--env-file", str(root / ".env"),
               "-f", str(root / "deploy/compose.yaml")]
    report = {"status": "running", "checks": []}
    try:
        wait_for_ready(args.base_url)
        for cycle, service, dependency in [(cycle, service, dependency) for cycle in range(1, args.cycles + 1) for service, dependency in [("redis", "redis"), ("minio", "object_storage")]]:
            record = {"cycle": cycle, "service": service, "status": "running"}
            report["checks"].append(record)
            try:
                subprocess.run(command + ["stop", "--timeout", "5", service], check=True)
                started = time.monotonic()
                status, body = get(args.base_url, "/health/ready")
                elapsed = time.monotonic() - started
                if status != 503 or body["dependencies"].get(dependency) != "unavailable":
                    raise RuntimeError("Stopped dependency was not reported unavailable")
                if elapsed >= 10 or get(args.base_url, "/health/live")[0] != 200:
                    raise RuntimeError("Readiness blocked or liveness failed")
                record.update(status="passed", unavailable_in_ms=round(elapsed * 1000), live_during_outage=True)
            finally:
                subprocess.run(command + ["start", service], check=True)
                wait_for_ready(args.base_url)
            record["recovered"] = True
            print(f"PASS {service} failure and recovery", flush=True)
        report["status"] = "passed"
    finally:
        if report["status"] == "running":
            report["status"] = "failed"
        output = root / args.report
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
