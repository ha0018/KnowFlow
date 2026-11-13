"""Kill a real disposable Celery worker five times and verify fenced recovery."""

import json
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ["docker", "compose", "--env-file", str(ROOT / ".env"), "-f", str(ROOT / "deploy/compose.yaml")]


def compose(*args, **kwargs):
    return subprocess.run([*COMPOSE, *args], cwd=ROOT, check=True, **kwargs)


def fixture(action, task_id=None):
    args = [
        "exec",
        "-T",
        "backend",
        ".venv/bin/python",
        "manage.py",
        "worker_fault_fixture",
        action,
        "--disposable",
    ]
    if task_id:
        args += ["--task-id", task_id]
    return json.loads(compose(*args, capture_output=True, text=True).stdout)


def main():
    report = {
        "scope": "disposable_real_Celery_SIGKILL_and_forced_lease_expiry",
        "status": "running",
        "cases": [],
    }
    output = ROOT / ".runtime/worker-recovery.json"
    try:
        for number in range(5):
            record = {"iteration": number + 1, "status": "running"}
            report["cases"].append(record)
            paused = False
            try:
                compose("pause", "worker")
                paused = True
                task = fixture("prepare")
                record["task_id"] = task["task_id"]
                for _ in range(25):
                    compose("unpause", "worker")
                    paused = False
                    time.sleep(0.1)
                    compose("pause", "worker")
                    paused = True
                    state = fixture("status", task["task_id"])
                    if state["status"] == "RUNNING":
                        break
                    if state["status"] in {"FAILED", "READY", "CANCELLED"}:
                        raise RuntimeError("Fixture finished before interruption")
                else:
                    raise RuntimeError("Worker did not claim fixture")
                record["before"] = state
                compose("unpause", "worker")
                paused = False
                compose("kill", "--signal", "SIGKILL", "worker")
                interrupted = fixture("status", task["task_id"])
                if interrupted["status"] != "RUNNING":
                    raise RuntimeError("Fixture was not interrupted while RUNNING")
                fixture("expire", task["task_id"])
                record["lease_expiry_forced"] = True
                started = time.monotonic()
                compose("up", "-d", "--no-deps", "worker")
                deadline = time.monotonic() + 180
                while time.monotonic() < deadline:
                    after = fixture("status", task["task_id"])
                    if after["status"] in {"READY", "FAILED", "CANCELLED"}:
                        break
                    time.sleep(1)
                record["after"] = after
                record["recovery_ms_after_forced_expiry"] = round((time.monotonic() - started) * 1000)
                if (
                    after["status"] != "READY"
                    or after["attempt"] < 2
                    or after["fence_token"] <= state["fence_token"]
                    or after["chunks"] != after["expected_chunks"]
                    or after["active_build"] != after["build_id"]
                ):
                    raise RuntimeError("Interrupted document did not recover consistently")
                record["status"] = "passed"
                print(f"PASS worker interruption {number + 1}", flush=True)
            finally:
                if paused:
                    compose("unpause", "worker")
                compose("up", "-d", "--no-deps", "worker")
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        report["status"] = "passed"
    finally:
        if report["status"] == "running":
            report["status"] = "failed"
        output.write_text(json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
