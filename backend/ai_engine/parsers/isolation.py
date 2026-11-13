import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from django.conf import settings

from .core import ParseError, validate_file


def parser_limits():
    limits = {
        "seconds": settings.PARSER_TIMEOUT_SECONDS,
        "cpu_seconds": settings.PARSER_CPU_SECONDS,
        "memory_mb": settings.PARSER_MEMORY_MB,
        "output_bytes": settings.PARSER_OUTPUT_BYTES,
    }
    if not (
        0.1 <= limits["seconds"] <= 120
        and type(limits["cpu_seconds"]) is int
        and 1 <= limits["cpu_seconds"] <= 120
        and type(limits["memory_mb"]) is int
        and 128 <= limits["memory_mb"] <= 2048
        and type(limits["output_bytes"]) is int
        and 1024 <= limits["output_bytes"] <= 32 * 1024**2
    ):
        raise ParseError("PARSER_CONFIG_INVALID")
    return limits


def parse(name, raw, *, check=None):
    validate_file(name, raw)
    limits = parser_limits()
    deadline = time.monotonic() + limits["seconds"]
    # No database/model/storage credentials or application settings are inherited.
    env = {k: os.environ[k] for k in ("SystemRoot", "WINDIR", "PATH") if k in os.environ}
    env.update(PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    with tempfile.TemporaryDirectory(prefix="knowflow-parse-") as folder:
        directory = Path(folder)
        source, output = directory / "source", directory / "result.json"
        source.write_bytes(raw)
        config = {
            key: getattr(settings, key)
            for key in ("MAX_UPLOAD_BYTES", "MAX_DOCUMENT_PAGES", "MAX_PARSED_CHARS")
        }
        payload = json.dumps(
            {"name": name, "source": str(source), "limits": limits, "config": config}
        ).encode()
        with output.open("wb") as target:
            process = subprocess.Popen(
                [sys.executable, "-I", str(Path(__file__).with_name("worker.py"))],
                cwd=directory,
                env=env,
                stdin=subprocess.PIPE,
                stdout=target,
                stderr=subprocess.DEVNULL,
            )
            try:
                process.stdin.write(payload)
                process.stdin.close()
                while process.poll() is None:
                    if check:
                        check()
                    if time.monotonic() >= deadline:
                        raise ParseError("PARSER_TIMEOUT")
                    if output.stat().st_size > limits["output_bytes"]:
                        raise ParseError("PARSER_OUTPUT_LIMIT")
                    time.sleep(min(0.05, max(0, deadline - time.monotonic())))
                if output.stat().st_size > limits["output_bytes"]:
                    raise ParseError("PARSER_OUTPUT_LIMIT")
                if process.returncode != 0:
                    raise ParseError("PARSER_RESOURCE_LIMIT")
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait()
        try:
            value = json.loads(output.read_bytes())
        except (ValueError, UnicodeDecodeError) as exc:
            raise ParseError("PARSER_PROTOCOL_INVALID") from exc
        if "error" in value:
            raise ParseError(value["error"])
        blocks = value.get("blocks")
        if not isinstance(blocks, list) or not blocks or len(blocks) > 20000:
            raise ParseError("PARSER_OUTPUT_LIMIT")
        if check:
            check()
        return blocks
