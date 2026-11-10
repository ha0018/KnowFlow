import hashlib
import os
import platform
import subprocess

from django.conf import settings
from django.db import connection

from ai_engine.gateway.config import profile
from ai_engine.retrieval.options import baseline


def environment():
    try:
        memory = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (AttributeError, ValueError, OSError):
        memory = None
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "database": connection.vendor,
        "processor": platform.processor(),
        "cpu_count": os.cpu_count(),
        "memory_bytes": memory,
        "dependency_lock_sha256": hashlib.sha256((settings.BASE_DIR / "uv.lock").read_bytes()).hexdigest(),
    }


def runtime_signature():
    model = profile("generation")
    return {
        "embedding_profile": settings.EMBEDDING_PROFILE,
        "generation_provider": model.provider,
        "generation_model": model.model,
        "generation_endpoint_hash": hashlib.sha256(model.base_url.encode()).hexdigest()[:16],
        "input_budget": settings.GENERATION_INPUT_BUDGET,
        "output_budget": settings.GENERATION_MAX_OUTPUT_TOKENS,
        "rewrite_provider": settings.REWRITE_PROVIDER,
        "reranker_provider": settings.RERANKER_PROVIDER,
        "reranker_model": settings.RERANKER_MODEL,
        "reranker_max_chars": settings.RERANKER_MAX_CHARS,
        "reranker_endpoint_hash": hashlib.sha256(settings.RERANKER_BASE_URL.encode()).hexdigest()[:16],
    }


def snapshot(dataset, name="B1", split="tuning", answers=False, rewrite=False):
    from apps.monitoring.metering import limits

    options = baseline(name, rewrite=rewrite)
    return {
        "baseline": name,
        "mode": options.mode,
        "retrieval_options": options.snapshot(),
        "split": split,
        "answers": answers,
        "runtime": runtime_signature(),
        "dataset_version": dataset.version,
        "corpus_manifest_hash": dataset.corpus_manifest_hash,
        "source_sha256": dataset.manifest.get("source_sha256"),
        "case_hash": dataset.manifest.get("case_hash"),
        "prompt": "grounded-v2",
        "metric_version": "evidence-spans-v1",
        "environment": environment(),
        "real_supplier_verified": False,
        "quota_defaults": {
            "tenant_daily": settings.DEFAULT_TENANT_DAILY_TOKENS,
            "user_daily": settings.DEFAULT_USER_DAILY_TOKENS,
        },
        "quota_limits": limits(dataset.tenant),
    }


def revision():
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=settings.BASE_DIR,
            capture_output=True,
            text=True,
            timeout=3,
            check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=settings.BASE_DIR,
            capture_output=True,
            text=True,
            timeout=3,
            check=True,
        ).stdout
        return commit + ("+dirty" if dirty else "")
    except (OSError, subprocess.SubprocessError):
        return os.environ.get("CODE_REVISION", "unavailable")
