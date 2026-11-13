"""Self-authored disposable acceptance documents; no outside corpus or credentials."""

import io
import zipfile

from docx import Document
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject


def pdf(text="Refund requests are accepted within 30 days.", pages=1, *, encrypted=False, blank=False):
    writer = PdfWriter()
    for _ in range(pages):
        page = writer.add_blank_page(width=300, height=300)
        if not blank:
            font = DictionaryObject(
                {
                    NameObject("/Type"): NameObject("/Font"),
                    NameObject("/Subtype"): NameObject("/Type1"),
                    NameObject("/BaseFont"): NameObject("/Helvetica"),
                }
            )
            page[NameObject("/Resources")] = DictionaryObject(
                {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
            )
            stream = DecodedStreamObject()
            escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            stream.set_data(f"BT /F1 12 Tf 20 200 Td ({escaped}) Tj ET".encode("ascii"))
            page[NameObject("/Contents")] = writer._add_object(stream)
    if encrypted:
        writer.encrypt("fixture-password")
    result = io.BytesIO()
    writer.write(result)
    return result.getvalue()


def docx(text="退款在30天内可以申请。"):
    document = Document()
    document.add_heading("自编验收资料", 1)
    document.add_paragraph(text)
    stream = io.BytesIO()
    document.save(stream)
    return stream.getvalue()


def zipped(values):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name, raw in values.items():
            archive.writestr(name, raw)
    return stream.getvalue()


def valid_documents():
    for i in range(50):
        text = f"Acceptance policy {i}: refund requests are accepted within 30 days."
        ext = ["pdf", "docx", "txt", "md"][i % 4]
        raw = pdf(text) if ext == "pdf" else docx(text) if ext == "docx" else text.encode()
        yield f"valid-{i:02}.{ext}", raw


def invalid_documents():
    return [
        ("program.exe", b"MZ", "UNSUPPORTED_TYPE"),
        ("page.html", b"<html/>", "UNSUPPORTED_TYPE"),
        ("sheet.xlsx", b"fake", "UNSUPPORTED_TYPE"),
        ("empty.txt", b"", "EMPTY_FILE"),
        ("empty.md", b"", "EMPTY_FILE"),
        ("empty.pdf", b"", "EMPTY_FILE"),
        ("fake.pdf", b"invalid", "INVALID_PDF"),
        ("fake.docx", b"invalid", "INVALID_DOCX"),
        ("encoding.txt", b"\xff", "UTF8_REQUIRED"),
        ("binary.md", b"\x00binary", "BINARY_TEXT"),
        ("spaces.txt", b" \n ", "EMPTY_TEXT"),
        ("spaces.md", b" \n ", "EMPTY_TEXT"),
        ("scan.pdf", pdf(blank=True), "OCR_REQUIRED"),
        ("encrypted.pdf", pdf(encrypted=True), "ENCRYPTED_PDF"),
        ("broken.pdf", b"%PDF-1.7\ninvalid", "INVALID_DOCUMENT"),
        ("incomplete.docx", zipped({"other.xml": b"<x/>"}), "INVALID_DOCX"),
        (
            "macro.docx",
            zipped({"word/document.xml": b"<x/>", "word/vbaProject.bin": b"macro"}),
            "MACROS_NOT_ALLOWED",
        ),
        ("broken.docx", zipped({"word/document.xml": b"not xml"}), "INVALID_DOCUMENT"),
        ("no-pages.pdf", pdf(pages=0), "OCR_REQUIRED"),
        ("encoding.md", b"\xff\xfe", "UTF8_REQUIRED"),
    ]
