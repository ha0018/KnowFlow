import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import F
from rest_framework.exceptions import ValidationError

from ai_engine.gateway.budget import prepare_input
from ai_engine.retrieval import search
from ai_engine.retrieval.options import baseline
from apps.agents.services import execute
from apps.documents.selectors import documents
from apps.evaluation.corpus import verify_frozen
from apps.evaluation.models import EvalDataset
from apps.knowledge.models import KnowledgeBaseACL
from apps.users.models import Tenant, User
from common.context import RequestContext


class Command(BaseCommand):
    help = "Run an independent 50-case retrieval/input/tool boundary matrix on the synthetic S3 fixtures"

    def add_arguments(self, parser):
        parser.add_argument("--email", default="admin@acme.demo")
        parser.add_argument("--output", required=True)

    def handle(self, *args, **options):
        actor = User.objects.filter(
            email=options["email"], is_tenant_admin=True, is_active=True, tenant__is_active=True
        ).first()
        if not actor:
            raise CommandError("Active tenant administrator required")
        dataset = EvalDataset.objects.get(tenant=actor.tenant, version="quality-v1", split="stratified")
        verify_frozen(dataset)
        fixture = dataset.manifest["fixture_documents"]
        subjects = {
            role: User.objects.get(pk=pk, tenant=actor.tenant)
            for role, pk in dataset.manifest["actors"].items()
        }
        results = []
        probes = [
            ("public", "退款细则退款期限", fixture["refund"]),
            ("private", "管理层奖金核验标识", fixture["private"]),
            ("restricted", "研发专属方案核验标识", fixture["restricted"]),
            ("foreign", "远山专属方案核验标识", fixture["foreign"]),
        ]

        def probe(role, name, scope, query, target):
            ctx, trace = RequestContext.for_user(subjects[role]), {}
            try:
                hits = search(ctx, query, options=baseline(name), diagnostics=trace)
                allowed = set(map(str, documents(ctx).values_list("pk", flat=True)))
                leaked = [h["document_id"] for h in hits if h["document_id"] not in allowed]
                # This inspects the actual generation input builder, not a claim
                # about the behavior of an external language model.
                prepared = prepare_input(query, hits)
                forbidden = {
                    "S3-PRIVATE-7391": fixture["private"],
                    "S3-RESTRICTED-8524": fixture["restricted"],
                    "S3-FOREIGN-9642": fixture["foreign"],
                }
                payload = json.dumps(prepared.messages, ensure_ascii=False)
                leaked += [
                    doc for marker, doc in forbidden.items() if doc not in allowed and marker in payload
                ]
                results.append(
                    {
                        "actor": role,
                        "baseline": name,
                        "scope": scope,
                        "target_allowed": target in allowed,
                        "retrieved_count": len(hits),
                        "unauthorized_occurrences": len(leaked),
                        "status": "passed" if not leaked else "failed",
                        "trace_id": ctx.trace_id,
                    }
                )
            except Exception:
                results.append(
                    {
                        "actor": role,
                        "baseline": name,
                        "scope": scope,
                        "status": "failed",
                        "error": "MATRIX_PROBE_FAILED",
                    }
                )

        for role in subjects:
            for name in ("B0", "B1", "B2"):
                for scope, query, target in probes:
                    probe(role, name, scope, query, target)
        # A real grant -> revoke -> restore transition, confined to fixture users.
        grant = KnowledgeBaseACL.objects.get(
            user=subjects["editor"], knowledge_base__documents__id=fixture["private"]
        )
        grant_values = {
            "tenant_id": grant.tenant_id,
            "knowledge_base_id": grant.knowledge_base_id,
            "user_id": grant.user_id,
            "access_level": grant.access_level,
        }
        try:
            with transaction.atomic():
                grant.delete()
                Tenant.objects.filter(pk=actor.tenant_id).update(acl_epoch=F("acl_epoch") + 1)
            for name in ("B0", "B1", "B2"):
                probe("editor", name, "revoked", "管理层奖金核验标识", fixture["private"])
        finally:
            with transaction.atomic():
                KnowledgeBaseACL.objects.get_or_create(**grant_values)
                Tenant.objects.filter(pk=actor.tenant_id).update(acl_epoch=F("acl_epoch") + 1)
        # Repeat revoked state for a second scope to keep grant types distinct.
        from apps.documents.models import DocumentACL

        grant = DocumentACL.objects.get(user=subjects["editor"], document_id=fixture["restricted"])
        grant_values = {
            "tenant_id": grant.tenant_id,
            "document_id": grant.document_id,
            "user_id": grant.user_id,
            "access_level": grant.access_level,
        }
        try:
            with transaction.atomic():
                grant.delete()
                Tenant.objects.filter(pk=actor.tenant_id).update(acl_epoch=F("acl_epoch") + 1)
            for name in ("B0", "B1", "B2"):
                probe("editor", name, "document_revoked", "研发专属方案核验标识", fixture["restricted"])
        finally:
            with transaction.atomic():
                DocumentACL.objects.get_or_create(**grant_values)
                Tenant.objects.filter(pk=actor.tenant_id).update(acl_epoch=F("acl_epoch") + 1)
        actor.refresh_from_db()
        ctx = RequestContext.for_user(actor)
        attacks = [
            ("ShellTool", {"command": "whoami"}),
            ("KnowledgeSearchTool", {"query": "退款", "tenant_id": "forged"}),
            ("KnowledgeSearchTool", {"query": "退款", "top_k": 1000}),
            ("DataAnalysisTool", {"date_from": "2026-01-01", "date_to": "2026-12-01", "group_by": "sql"}),
            ("DocumentSummaryTool", {"document_id": fixture["foreign"], "tenant_id": str(actor.tenant_id)}),
            ("KnowledgeSearchTool", {"query": "退款", "provider_url": "http://invalid"}),
            ("DataAnalysisTool", {"date_from": "2020-01-01", "date_to": "2026-12-01", "group_by": "month"}),
            ("DocumentSummaryTool", {"document_id": "not-a-uuid"}),
        ]
        for n, (tool, args) in enumerate(attacks, 1):
            try:
                execute(ctx, tool, args)
                status = "failed"
            except ValidationError:
                status = "passed"
            except Exception:
                status = "failed"
            results.append({"scope": "tool_schema", "case": n, "status": status})
        report = {
            "schema_version": 1,
            "scope": "independent_retrieval_prompt_input_and_tool_schema_matrix",
            "not_verified": ["real_model_prompt_injection_resistance", "all_A01_100_authorization_cases"],
            "cases": results,
            "case_count": len(results),
            "failed": sum(r["status"] != "passed" for r in results),
            "unauthorized_occurrences": sum(r.get("unauthorized_occurrences", 0) for r in results),
        }
        output = Path(options["output"])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.stdout.write(json.dumps({k: v for k, v in report.items() if k != "cases"}, ensure_ascii=True))
        if report["failed"]:
            raise CommandError("Security matrix has failures; inspect the retained report")
