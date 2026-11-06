import io
from datetime import timedelta

import pytest
from django.utils import timezone
from pypdf import PdfWriter

from ai_engine.chunking import split_blocks
from ai_engine.parsers import ParseError, parse, validate_file
from apps.documents.models import DocumentChunk, DocumentTask
from apps.documents.services import claim, process

pytestmark = pytest.mark.django_db(transaction=True)


def test_pipeline_publishes_and_duplicate_delivery_is_idempotent(ingest):
    doc, _, build, task = ingest()
    doc.refresh_from_db()
    task.refresh_from_db()
    assert doc.active_build_id == build.pk and task.status == "READY"
    before = list(DocumentChunk.objects.filter(build=build).values_list("pk", "content_hash"))
    process(task.pk)
    assert list(DocumentChunk.objects.filter(build=build).values_list("pk", "content_hash")) == before


def test_failed_new_version_preserves_old_active_build(ingest):
    doc, _, old, _ = ingest()
    _, _, failed, task = ingest(text="   ", document_id=doc.pk)
    doc.refresh_from_db()
    failed.refresh_from_db()
    task.refresh_from_db()
    assert failed.status == "FAILED" and task.error_code == "EMPTY_TEXT"
    assert doc.active_build_id == old.pk


def test_obsolete_build_cannot_replace_newer_build(ingest, settings):
    doc, _, old, _ = ingest()
    settings.CELERY_TASK_ALWAYS_EAGER = False
    from unittest.mock import patch

    with patch("apps.documents.services.dispatch"):
        _, _, first, first_task = ingest(text="退款期限更新为40天。", document_id=doc.pk)
        _, _, second, second_task = ingest(text="退款期限更新为50天。", document_id=doc.pk)
    process(second_task.pk)
    process(first_task.pk)
    doc.refresh_from_db()
    first_task.refresh_from_db()
    assert doc.active_build_id == second.pk and doc.active_build_id != old.pk
    assert first_task.status == "CANCELLED"


def test_fencing_rejects_late_worker(ingest, monkeypatch):
    from unittest.mock import patch

    with patch("apps.documents.services.dispatch"):
        doc, _, build, task = ingest()

    def stale_embedding(texts, **kwargs):
        DocumentTask.objects.filter(pk=task.pk).update(fence_token=99, status="RUNNING")
        return [[1.0] * 128 for _ in texts], {"source": "unknown"}

    monkeypatch.setattr("apps.documents.services.embed", stale_embedding)
    process(task.pk)
    doc.refresh_from_db()
    task.refresh_from_db()
    assert doc.active_build_id is None
    assert task.fence_token == 99 and task.status == "RUNNING"
    assert build.chunks.count() == 0


def test_expired_lease_can_be_reclaimed(ingest):
    from unittest.mock import patch

    with patch("apps.documents.services.dispatch"):
        _, _, _, task = ingest()
    first = claim(task.pk)
    assert claim(task.pk) is None
    DocumentTask.objects.filter(pk=task.pk).update(lease_until=timezone.now() - timedelta(seconds=1))
    second = claim(task.pk)
    assert second.fence_token == first.fence_token + 1


@pytest.mark.parametrize(
    "name,raw,code",
    [
        ("a.exe", b"MZ", "UNSUPPORTED_TYPE"),
        ("a.pdf", b"bad", "INVALID_PDF"),
        ("a.md", b"\x00bad", "BINARY_TEXT"),
        ("a.txt", b"\xff", "UTF8_REQUIRED"),
        ("a.md", b"", "EMPTY_FILE"),
        ("a.docx", b"fake", "INVALID_DOCX"),
    ],
)
def test_invalid_files_are_explicit(name, raw, code):
    with pytest.raises(ParseError, match=code):
        validate_file(name, raw)


def test_scanned_pdf_reports_ocr_required():
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    raw = io.BytesIO()
    writer.write(raw)
    with pytest.raises(ParseError, match="OCR_REQUIRED"):
        parse("scan.pdf", raw.getvalue())


def test_docx_uses_paragraphs_not_fabricated_pages():
    from docx import Document

    doc = Document()
    doc.add_heading("说明", 1)
    doc.add_paragraph("知识库支持权限控制。")
    raw = io.BytesIO()
    doc.save(raw)
    blocks = parse("manual.docx", raw.getvalue())
    assert blocks[-1]["locator"]["paragraph"] == 2
    assert "page_start" not in blocks[-1]["locator"]


def test_chunk_locators_are_deterministic():
    blocks = [{"text": "知识库文档权限" * 200, "locator": {"page_start": 2}}]
    first = split_blocks(blocks)
    assert first == split_blocks(blocks)
    assert all(c["token_count"] <= 600 for c in first)
    for c in first:
        pos = c["locator"]
        assert c["content"] == blocks[0]["text"][pos["char_start"] : pos["char_end"]]
