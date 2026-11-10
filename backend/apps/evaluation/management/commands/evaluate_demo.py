import hashlib
import json
import platform
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.evaluation.models import EvalDataset, EvalRun
from apps.evaluation.services import perform_evaluation
from apps.users.models import User
from apps.users.permissions import require_capability
from common.context import RequestContext


class Command(BaseCommand):
    help = "Run a development retrieval evaluation and export configuration plus per-case results"

    def add_arguments(self, parser):
        parser.add_argument("--email", default="admin@acme.demo")
        parser.add_argument("--mode", choices=["dense", "hybrid"], default="hybrid")
        parser.add_argument("--output", required=True)

    def handle(self, *args, **options):
        user = User.objects.filter(email=options["email"], is_active=True, tenant__is_active=True).first()
        if not user:
            raise CommandError("Active evaluation user not found; seed the demo first")
        ctx = RequestContext.for_user(user)
        require_capability(ctx, "evaluation.run")
        dataset = EvalDataset.objects.filter(tenant=user.tenant, split="development").first()
        if not dataset:
            raise CommandError("Dataset not found")
        config = {
            "mode": options["mode"],
            "embedding_profile": settings.EMBEDDING_PROFILE,
            "dataset_version": dataset.version,
            "corpus_manifest_hash": dataset.corpus_manifest_hash,
            "python": platform.python_version(),
            "platform": platform.platform(),
            "dependency_lock_sha256": hashlib.sha256(
                (settings.BASE_DIR / "uv.lock").read_bytes()
            ).hexdigest(),
        }
        run = EvalRun.objects.create(
            tenant=user.tenant,
            dataset=dataset,
            actor=user,
            trace_id=ctx.trace_id,
            config_snapshot=config,
            code_revision="workspace-v0.1.0",
        )
        perform_evaluation(run.pk)
        run.refresh_from_db()
        report = {
            "run_id": str(run.pk),
            "status": run.status,
            "config": config,
            "metrics": run.metrics,
            "started_at": run.started_at.isoformat(),
            "finished_at": run.finished_at.isoformat(),
            "results": [
                {
                    "case_id": str(r.case_id),
                    "question": r.case.question,
                    "retrieved_ids": r.retrieved_ids,
                    "metrics": r.metrics,
                    "error": r.error,
                }
                for r in run.results.select_related("case")
            ],
        }
        output = Path(options["output"])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        self.stdout.write(json.dumps(run.metrics, ensure_ascii=True))
