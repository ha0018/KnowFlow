import hashlib
import json
from pathlib import Path

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management.base import BaseCommand, CommandError
from django.test.utils import override_settings

from apps.documents.models import DocumentACL
from apps.documents.services import upload
from apps.evaluation.corpus import case_state, corpus_state, digest, verify_frozen
from apps.evaluation.models import EvalCase, EvalDataset
from apps.knowledge.models import KnowledgeBase, KnowledgeBaseACL
from apps.users.models import Tenant, User
from common.context import RequestContext


class Command(BaseCommand):
    help = "Import the fixed 100-case S3 corpus without modifying existing datasets or passwords"

    def add_arguments(self, parser):
        parser.add_argument("--email", default="admin@acme.demo")
        parser.add_argument(
            "--password", required=True, help="Password for newly created synthetic fixture users"
        )
        parser.add_argument(
            "--fixture", default=str(settings.BASE_DIR.parent / "docs/datasets/quality-v1.json")
        )

    def handle(self, *args, **options):
        actor = User.objects.filter(
            email=options["email"], is_active=True, is_tenant_admin=True, tenant__is_active=True
        ).first()
        if not actor or len(options["password"]) < 8:
            raise CommandError("Active tenant administrator and 8+ character fixture password required")
        path = Path(options["fixture"])
        raw = path.read_bytes()
        source_hash = hashlib.sha256(raw).hexdigest()
        if path.with_suffix(".sha256").read_text().strip() != source_hash:
            raise CommandError("FIXTURE_CHECKSUM_MISMATCH")
        fixture = json.loads(raw)
        existing = EvalDataset.objects.filter(
            tenant=actor.tenant, name=fixture["name"], version=fixture["version"]
        ).first()
        if existing:
            if existing.manifest.get("source_sha256") != source_hash:
                raise CommandError("FIXTURE_VERSION_REUSED")
            verify_frozen(existing)
            self.stdout.write(f"Dataset already frozen: {existing.pk}")
            return
        users = {"admin": actor}
        for role in ("reader", "editor"):
            email = f"quality-{role}@{actor.tenant.slug}.demo"
            user, created = User.objects.get_or_create(
                email=email, defaults={"tenant": actor.tenant, "display_name": f"S3 {role}"}
            )
            if user.tenant_id != actor.tenant_id:
                raise CommandError("FIXTURE_USER_TENANT_MISMATCH")
            if created:
                user.set_password(options["password"])
                user.save()
            users[role] = user
        foreign, _ = Tenant.objects.get_or_create(slug="quality-foreign", defaults={"name": "S3隔离租户"})
        outsider, created = User.objects.get_or_create(
            email="admin@quality-foreign.demo", defaults={"tenant": foreign, "is_tenant_admin": True}
        )
        if outsider.tenant_id != foreign.pk:
            raise CommandError("FIXTURE_USER_TENANT_MISMATCH")
        if created:
            outsider.set_password(options["password"])
            outsider.save()
        bases = {}
        for scope in ("public", "private", "restricted", "foreign"):
            owner = outsider if scope == "foreign" else actor
            kb, _ = KnowledgeBase.objects.get_or_create(
                tenant=owner.tenant,
                name=f"S3质量语料-{scope}",
                defaults={"owner": owner, "visibility": "private" if scope == "private" else "tenant"},
            )
            bases[scope] = kb
        KnowledgeBaseACL.objects.get_or_create(
            tenant=actor.tenant,
            knowledge_base=bases["private"],
            user=users["editor"],
            defaults={"access_level": "read"},
        )
        docs = {}
        with override_settings(CELERY_TASK_ALWAYS_EAGER=True):
            for spec in fixture["documents"]:
                kb = bases[spec["scope"]]
                owner = outsider if spec["scope"] == "foreign" else actor
                doc = kb.documents.filter(title=spec["title"]).first()
                if not doc:
                    doc, _, _, _ = upload(
                        RequestContext.for_user(owner),
                        kb.pk,
                        SimpleUploadedFile(
                            f"{spec['key']}.md", spec["body"].encode(), content_type="text/markdown"
                        ),
                        spec["title"],
                    )
                    if spec["scope"] == "restricted":
                        doc.acl_mode = "restricted"
                        doc.save(update_fields=["acl_mode"])
                        DocumentACL.objects.get_or_create(
                            tenant=actor.tenant,
                            document=doc,
                            user=users["editor"],
                            defaults={"access_level": "read"},
                        )
                doc.refresh_from_db()
                if not doc.active_build_id or doc.active_build.status != "READY":
                    raise CommandError("CORPUS_NOT_READY")
                if doc.active_build.version.sha256 != hashlib.sha256(spec["body"].encode()).hexdigest():
                    raise CommandError("CORPUS_FIXTURE_CHANGED")
                docs[spec["key"]] = doc
        own_ids = [str(d.pk) for d in docs.values() if d.tenant_id == actor.tenant_id]
        state = corpus_state(actor.tenant_id, own_ids)
        dataset = EvalDataset.objects.create(
            tenant=actor.tenant,
            name=fixture["name"],
            version=fixture["version"],
            corpus_manifest_hash=digest(state),
            split="stratified",
        )
        for spec in fixture["cases"]:
            gold = []
            for evidence in spec["gold"]:
                doc = docs[evidence["document"]]
                build = doc.active_build
                matches = [
                    (n, block["text"].index(evidence["quote"]))
                    for n, block in enumerate(build.parsed_blocks)
                    if evidence["quote"] in block["text"]
                ]
                if len(matches) != 1:
                    raise CommandError("GOLD_SPAN_NOT_UNIQUE")
                block, start = matches[0]
                gold.append(
                    {
                        "document_id": str(doc.pk),
                        "version_id": str(build.version_id),
                        "sha256": build.version.sha256,
                        "block": block,
                        "char_start": start,
                        "char_end": start + len(evidence["quote"]),
                        "quote": evidence["quote"],
                    }
                )
            EvalCase.objects.create(
                tenant=actor.tenant,
                dataset=dataset,
                case_key=spec["key"],
                question=spec["question"],
                expected_answer=spec["expected_answer"],
                category=spec["category"],
                split=spec["split"],
                actor_fixture=spec["actor"],
                gold_evidence=gold,
                spec={
                    k: v
                    for k, v in spec.items()
                    if k not in {"key", "question", "expected_answer", "category", "split", "actor", "gold"}
                },
            )
        dataset.manifest = {
            "source_sha256": source_hash,
            "document_ids": sorted(own_ids),
            "kb_ids": [str(kb.pk) for scope, kb in bases.items() if scope != "foreign"],
            "actors": {key: str(user.pk) for key, user in users.items()},
            "case_hash": digest(case_state(dataset.cases.all())),
            "fixture_documents": {key: str(doc.pk) for key, doc in docs.items()},
            "scope": "synthetic_self_authored",
            "created_by": str(actor.pk),
        }
        dataset.save(update_fields=["manifest"])
        self.stdout.write(f"Frozen dataset {dataset.pk}: 100 cases / 40 tuning / 60 holdout")
