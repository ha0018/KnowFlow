import io
import re
import zipfile
from pathlib import Path

from django.conf import settings
from docx import Document as DocxDocument
from pypdf import PdfReader

MIME_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain",
    ".md": "text/markdown",
}


class ParseError(ValueError):
    pass


def validate_file(name, raw, *, inspect_archive=True):
    ext = Path(name).suffix.lower()
    if ext not in MIME_TYPES:
        raise ParseError("UNSUPPORTED_TYPE")
    if not raw:
        raise ParseError("EMPTY_FILE")
    if len(raw) > settings.MAX_UPLOAD_BYTES:
        raise ParseError("FILE_TOO_LARGE")
    if ext == ".pdf" and not raw.startswith(b"%PDF-"):
        raise ParseError("INVALID_PDF")
    if ext == ".docx":
        if not raw.startswith(b"PK"):
            raise ParseError("INVALID_DOCX")
    if ext == ".docx" and inspect_archive:
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                if (
                    len(archive.infolist()) > 2000
                    or sum(i.file_size for i in archive.infolist()) > 50 * 1024 * 1024
                ):
                    raise ParseError("ARCHIVE_TOO_LARGE")
                if "word/document.xml" not in archive.namelist():
                    raise ParseError("INVALID_DOCX")
                if any("vbaproject" in n.lower() for n in archive.namelist()):
                    raise ParseError("MACROS_NOT_ALLOWED")
        except zipfile.BadZipFile as exc:
            raise ParseError("INVALID_DOCX") from exc
    if ext in {".md", ".txt"}:
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ParseError("UTF8_REQUIRED") from exc
        if "\x00" in text:
            raise ParseError("BINARY_TEXT")
    return ext, MIME_TYPES[ext]


def parse(name, raw):
    ext, _ = validate_file(name, raw)
    blocks = []
    try:
        if ext == ".pdf":
            reader = PdfReader(io.BytesIO(raw), strict=False)
            if reader.is_encrypted:
                raise ParseError("ENCRYPTED_PDF")
            if len(reader.pages) > settings.MAX_DOCUMENT_PAGES:
                raise ParseError("TOO_MANY_PAGES")
            total = 0
            for number, page in enumerate(reader.pages, 1):
                text = page.extract_text() or ""
                total += len(text)
                if total > settings.MAX_PARSED_CHARS:
                    raise ParseError("TEXT_TOO_LARGE")
                if text.strip():
                    blocks.append({"text": text, "locator": {"page_start": number, "page_end": number}})
            if not blocks:
                raise ParseError("OCR_REQUIRED")
        elif ext == ".docx":
            doc = DocxDocument(io.BytesIO(raw))
            heading = []
            for index, para in enumerate(doc.paragraphs):
                if para.style and para.style.name.startswith("Heading"):
                    heading = [para.text]
                if para.text.strip():
                    blocks.append(
                        {"text": para.text, "locator": {"paragraph": index + 1, "heading_path": heading[:]}}
                    )
            for number, table in enumerate(doc.tables, 1):
                rows = [" | ".join(cell.text for cell in row.cells) for row in table.rows]
                blocks.append({"text": "\n".join(rows), "locator": {"table": number}})
        else:
            heading = []
            text = raw.decode("utf-8-sig").replace("\r\n", "\n")
            for match in re.finditer(r"[^\n]+(?:\n(?!\n)[^\n]+)*", text):
                content = match.group()
                title = re.match(r"^#{1,6}\s+(.+)", content)
                if title:
                    heading = [title[1]]
                blocks.append(
                    {
                        "text": content,
                        "locator": {"heading_path": heading[:], "source_char_start": match.start()},
                    }
                )
    except ParseError:
        raise
    except Exception as exc:
        raise ParseError("INVALID_DOCUMENT") from exc
    if not blocks or not any(b["text"].strip() for b in blocks):
        raise ParseError("EMPTY_TEXT")
    if sum(len(b["text"]) for b in blocks) > settings.MAX_PARSED_CHARS:
        raise ParseError("TEXT_TOO_LARGE")
    return blocks
