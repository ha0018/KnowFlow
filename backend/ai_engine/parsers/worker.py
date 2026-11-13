"""Fixed isolated entry point. No project settings, database or credentials."""

import importlib.util
import json
import sys
from pathlib import Path


def apply_limits(limits):
    if sys.platform.startswith("linux"):
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (limits["memory_mb"] * 1024**2,) * 2)
        resource.setrlimit(resource.RLIMIT_CPU, (limits["cpu_seconds"], limits["cpu_seconds"] + 1))
        resource.setrlimit(resource.RLIMIT_FSIZE, (limits["output_bytes"],) * 2)
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))


def main():
    data = json.loads(sys.stdin.buffer.read(8192))
    limits = data["limits"]
    apply_limits(limits)
    from django.conf import settings

    settings.configure(**data["config"])
    spec = importlib.util.spec_from_file_location("parser_core", Path(__file__).with_name("core.py"))
    parser = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(parser)

    def forbid_external(event, args):
        if event.startswith("socket.") or event in {"subprocess.Popen", "os.system", "os.posix_spawn"}:
            raise PermissionError("Parser external access denied")

    sys.addaudithook(forbid_external)
    try:
        raw = Path(data["source"]).read_bytes()
        blocks = parser.parse(data["name"], raw)
        if len(blocks) > 20000:
            raise parser.ParseError("PARSER_OUTPUT_LIMIT")
        wire = json.dumps({"blocks": blocks}, ensure_ascii=False).encode()
        if len(wire) > limits["output_bytes"]:
            raise parser.ParseError("PARSER_OUTPUT_LIMIT")
    except MemoryError:
        wire = b'{"error":"PARSER_RESOURCE_LIMIT"}'
    except parser.ParseError as exc:
        wire = json.dumps({"error": str(exc)}).encode()
    except Exception:
        wire = b'{"error":"INVALID_DOCUMENT"}'
    sys.stdout.buffer.write(wire)


if __name__ == "__main__":
    main()
