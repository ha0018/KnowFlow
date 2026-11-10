import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.evaluation.configuration import revision, snapshot
from apps.evaluation.corpus import verify_frozen
from apps.evaluation.models import EvalDataset, EvalRun
from apps.evaluation.reporting import run_report
from apps.evaluation.services import perform_evaluation
from apps.users.models import User
from apps.users.permissions import require_capability
from common.context import RequestContext


class Command(BaseCommand):
    help = "Compare frozen B0/B1/B2 configurations; defaults to the tuning split, never to holdout"

    def add_arguments(self, parser):
        parser.add_argument("--email", default="admin@acme.demo")
        parser.add_argument("--dataset-version", default="quality-v1")
        parser.add_argument("--split", choices=["tuning", "holdout"], default="tuning")
        parser.add_argument("--baselines", nargs="+", choices=["B0", "B1", "B2"], default=["B0", "B1", "B2"])
        parser.add_argument(
            "--answers",
            action="store_true",
            help="Generate answers for human review; may call and bill configured models",
        )
        parser.add_argument(
            "--rewrite",
            action="store_true",
            help="Enable the same rewrite setting for every compared baseline",
        )
        parser.add_argument("--output", required=True)

    def handle(self, *args, **options):
        user = User.objects.filter(email=options["email"], is_active=True, tenant__is_active=True).first()
        if not user:
            raise CommandError("Active evaluation user not found")
        ctx = RequestContext.for_user(user)
        require_capability(ctx, "evaluation.run")
        dataset = EvalDataset.objects.filter(
            tenant=user.tenant, version=options["dataset_version"], split="stratified"
        ).first()
        if not dataset:
            raise CommandError("Run seed_quality first")
        verify_frozen(dataset)
        reports = []
        for name in options["baselines"]:
            run = EvalRun.objects.create(
                tenant=user.tenant,
                actor=user,
                dataset=dataset,
                trace_id=ctx.trace_id,
                code_revision=revision(),
                config_snapshot=snapshot(
                    dataset, name, options["split"], options["answers"], options["rewrite"]
                ),
            )
            perform_evaluation(run.pk)
            run.refresh_from_db()
            reports.append(run_report(ctx, run))
            self.stdout.write(f"{name} {run.status}: {json.dumps(run.metrics, ensure_ascii=True)}")
        report = {
            "schema_version": 1,
            "dataset_version": dataset.version,
            "split": options["split"],
            "source_sha256": dataset.manifest["source_sha256"],
            "corpus_manifest_hash": dataset.corpus_manifest_hash,
            "case_hash": dataset.manifest["case_hash"],
            "runs": reports,
            "acceptance": {
                "A06": "demo_only"
                if reports[0]["config"]["runtime"]["embedding_profile"].startswith("demo:")
                else "measured_not_automatically_accepted",
                "A07": "human_review_pending",
                "A08": "human_review_and_security_matrix_required",
                "A09": "load_protocol_pending",
                "A10": "separate_10000_chunk_index_report_required",
                "A11": "real_supplier_and_concurrent_load_pending",
                "A12": "real_embedding_pdf_timing_pending",
            },
        }
        output = Path(options["output"])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if any(r["status"] != "COMPLETED" for r in reports):
            raise CommandError("Evaluation had failures; all available results were retained in the report")
