"""Quiesced PostgreSQL/private-object backup and restore into a brand-new Compose project."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            result.update(block)
    return result.hexdigest()


class Deployment:
    def __init__(self, project, env_file, port=None):
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,50}", project):
            raise ValueError("Invalid Compose project name")
        self.project = project
        self.command = [
            "docker",
            "compose",
            "--project-name",
            project,
            "--env-file",
            str(Path(env_file).resolve()),
            "-f",
            str(ROOT / "deploy/compose.yaml"),
        ]
        self.env = dict(os.environ)
        if port is not None:
            self.env["HTTP_PORT"] = str(port)

    def run(self, *args, **kwargs):
        return subprocess.run([*self.command, *args], cwd=ROOT, env=self.env, check=True, **kwargs)

    def text(self, *args):
        return self.run(*args, capture_output=True, text=True).stdout.strip()

    def objects(self, directory, action, report=False):
        args = ["run", "--rm", "--no-deps"]
        if hasattr(os, "getuid"):
            args += ["--user", f"{os.getuid()}:{os.getgid()}"]
        args += [
            "--volume",
            f"{directory.resolve()}:/backup",
            "backend",
            ".venv/bin/python",
            "manage.py",
            "object_snapshot",
            action,
            "--archive",
            "/backup/objects.zip",
            "--quiesced",
        ]
        if report:
            args += ["--report", "/backup/restoration-report.json"]
        self.run(*args)


def backup(options):
    output = Path(options.output).resolve()
    if output.exists():
        raise ValueError("Backup output already exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    deployment = Deployment(options.project, options.env_file)
    writers = {"frontend", "backend", "worker", "scheduler"}
    running = set(deployment.text("ps", "--status", "running", "--services").splitlines())
    if "migrate" in running or not {"postgres", "minio"} <= running:
        raise ValueError("Deployment must be initialized and not migrating")
    stopped = sorted(writers & running)
    with tempfile.TemporaryDirectory(prefix="knowflow-backup-", dir=output.parent) as folder:
        directory = Path(folder)
        try:
            if stopped:
                deployment.run("stop", "--timeout", "130", *stopped)
            if set(deployment.text("ps", "--status", "running", "--services").splitlines()) & writers:
                raise ValueError("Application writers are still running")
            deployment.objects(directory, "export")
            dump = directory / "database.dump"
            with dump.open("xb") as target:
                deployment.run(
                    "exec",
                    "-T",
                    "postgres",
                    "pg_dump",
                    "-U",
                    "knowflow",
                    "-d",
                    "knowflow",
                    "--format=custom",
                    "--no-owner",
                    "--no-acl",
                    stdout=target,
                )
            manifest = {
                "schema": "knowflow-deployment-v1",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "source_project": options.project,
                "revision": os.environ.get("CODE_REVISION", "workspace"),
                "quiesced_services": stopped,
                "files": {
                    name: {"sha256": digest(directory / name), "size": (directory / name).stat().st_size}
                    for name in ("database.dump", "objects.zip")
                },
            }
            with zipfile.ZipFile(output, "x", zipfile.ZIP_STORED) as archive:
                for name in manifest["files"]:
                    archive.write(directory / name, name)
                archive.writestr("manifest.json", json.dumps(manifest, sort_keys=True))
            print(
                json.dumps(
                    {
                        "status": "passed",
                        "archive": str(output),
                        "sha256": digest(output),
                        "quiesced": stopped,
                    }
                )
            )
        finally:
            if stopped:
                deployment.run("up", "-d", "--no-deps", "--wait", *stopped)


def unpack(archive_path, directory):
    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()
        if len(names) != 3 or set(names) != {"database.dump", "objects.zip", "manifest.json"}:
            raise ValueError("Unexpected backup members")
        if (
            archive.getinfo("manifest.json").file_size > 65536
            or sum(i.file_size for i in archive.infolist()) > 4 * 1024**3
        ):
            raise ValueError("Oversized backup")
        manifest = json.loads(archive.read("manifest.json"))
        if manifest.get("schema") != "knowflow-deployment-v1" or set(manifest["files"]) != {
            "database.dump",
            "objects.zip",
        }:
            raise ValueError("Invalid backup manifest")
        for name, expected in manifest["files"].items():
            path = directory / name
            with archive.open(name) as source, path.open("xb") as target:
                while block := source.read(1024 * 1024):
                    target.write(block)
            if path.stat().st_size != expected["size"] or digest(path) != expected["sha256"]:
                raise ValueError("Backup checksum mismatch")
        return manifest


def restore(options):
    deployment = Deployment(options.new_project, options.env_file, options.port)
    # Read-only preflight: never clean or overwrite an existing target.
    if deployment.text("ps", "-a", "-q"):
        raise ValueError("Restore target already has containers")
    volumes = subprocess.run(
        [
            "docker",
            "volume",
            "ls",
            "--filter",
            "label=com.docker.compose.project=" + options.new_project,
            "-q",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if volumes:
        raise ValueError("Restore target already has persistent volumes")
    with tempfile.TemporaryDirectory(prefix="knowflow-restore-") as folder:
        directory = Path(folder)
        manifest = unpack(options.archive, directory)
        if manifest["source_project"] == options.new_project:
            raise ValueError("Restore project must differ from backup source")
        deployment.run("up", "-d", "--wait", "postgres", "redis", "minio", "bucket")
        with (directory / "database.dump").open("rb") as source:
            deployment.run(
                "exec",
                "-T",
                "postgres",
                "pg_restore",
                "-U",
                "knowflow",
                "-d",
                "knowflow",
                "--no-owner",
                "--no-acl",
                "--exit-on-error",
                stdin=source,
            )
        deployment.objects(directory, "import", report=True)
        report = json.loads((directory / "restoration-report.json").read_text())
        report.update(
            source_project=manifest["source_project"],
            restored_project=options.new_project,
            archive_sha256=digest(Path(options.archive)),
            restored_at=datetime.now(timezone.utc).isoformat(),
        )
        deployment.run("up", "-d", "--wait", "--wait-timeout", "240")
        if options.report:
            Path(options.report).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(
            json.dumps(
                {
                    "status": "passed",
                    "project": options.new_project,
                    "files": report["files"],
                    "database_digest_match": report["all_database_row_digests_match"],
                }
            )
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    create = sub.add_parser("backup")
    create.add_argument("--project", default="knowflow")
    create.add_argument("--env-file", default=str(ROOT / ".env"))
    create.add_argument("--output", required=True)
    recover = sub.add_parser("restore")
    recover.add_argument("--new-project", required=True)
    recover.add_argument("--env-file", required=True)
    recover.add_argument("--archive", required=True)
    recover.add_argument("--port", type=int, default=8081)
    recover.add_argument("--report")
    options = parser.parse_args()
    {"backup": backup, "restore": restore}[options.action](options)


if __name__ == "__main__":
    main()
