"""Verify the opt-in HTTPS deployment with an explicitly trusted test certificate."""

import argparse
import hashlib
import json
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--certificate", required=True)
    parser.add_argument("--base-url", default="https://localhost:8443")
    parser.add_argument("--report", default=".runtime/tls-report.json")
    options = parser.parse_args()
    context = ssl.create_default_context(cafile=options.certificate)
    records = []
    for path, expected in [
        ("/", 200),
        ("/health/live", 200),
        ("/health/ready", 200),
        ("/api/v1/auth/me", 401),
    ]:
        deadline = time.monotonic() + 60
        while True:
            try:
                try:
                    response = urllib.request.urlopen(options.base_url + path, context=context, timeout=5)
                except urllib.error.HTTPError as exc:
                    response = exc
                with response:
                    status = response.code
                    hsts = response.headers.get("Strict-Transport-Security")
                    response.read()
                if status != expected or not hsts:
                    raise RuntimeError("HTTPS status/header mismatch")
                records.append({"path": path, "status": "passed", "http_status": status, "hsts": hsts})
                break
            except (OSError, RuntimeError):
                if time.monotonic() >= deadline:
                    raise
                time.sleep(1)
    report = {
        "status": "passed",
        "scope": "CI_certificate_trusted_explicitly_not_public_CA_deployment",
        "certificate_sha256": hashlib.sha256(Path(options.certificate).read_bytes()).hexdigest(),
        "checks": records,
    }
    Path(options.report).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print("PASS HTTPS routing, authentication, health and HSTS")


if __name__ == "__main__":
    main()
