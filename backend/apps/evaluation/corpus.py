import hashlib
import json

from django.conf import settings

from apps.documents.models import Document


def digest(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def corpus_state(tenant_id, document_ids):
    state = []
    for doc in (
        Document.objects.filter(tenant_id=tenant_id, pk__in=document_ids)
        .select_related("active_build__version")
        .order_by("id")
    ):
        build = doc.active_build
        state.append(
            {
                "document_id": str(doc.pk),
                "build_id": str(doc.active_build_id),
                "version_id": str(build.version_id) if build else None,
                "sha256": build.version.sha256 if build else None,
                "profile": build.embedding_profile if build else None,
                "pipeline_hash": build.pipeline_hash if build else None,
                "ready": bool(build and build.status == "READY" and doc.deleted_at is None),
                "chunks": list(build.chunks.order_by("ordinal").values("ordinal", "content_hash", "locator"))
                if build
                else [],
            }
        )
    return state


def case_state(cases):
    return [
        {
            "key": c.case_key,
            "question": c.question,
            "category": c.category,
            "gold": c.gold_evidence,
            "actor": c.actor_fixture,
            "split": c.split,
            "expected": c.expected_answer,
            "spec": c.spec,
        }
        for c in sorted(cases, key=lambda c: c.case_key)
    ]


def verify_frozen(dataset):
    if not dataset.manifest:
        return  # Legacy development sets retain per-gold version checks.
    manifest = dataset.manifest
    state = corpus_state(dataset.tenant_id, manifest["document_ids"])
    if digest(state) != dataset.corpus_manifest_hash or not all(row["ready"] for row in state):
        raise ValueError("FROZEN_CORPUS_CHANGED")
    if any(row["profile"] != settings.EMBEDDING_PROFILE for row in state):
        raise ValueError("EMBEDDING_PROFILE_CHANGED")
    if digest(case_state(dataset.cases.all())) != manifest["case_hash"]:
        raise ValueError("FROZEN_DATASET_CHANGED")
