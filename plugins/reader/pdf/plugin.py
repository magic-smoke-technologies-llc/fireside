"""v2 reader plugin: pdf. The parse is host/pdf_import.py; this is the contract.

Order 6, ahead of epub: a PDF is the one format that announces itself in its
first five bytes, so it should claim the upload before a zip-shaped reader gets
a look at it.

A PDF with no text layer is refused with a reason rather than imported empty.
Refusing here is deliberate: OCR is a paid, separate activity, and silently
handing someone a book of nothing after they uploaded 300 pages is worse than
saying so.
"""
import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from host import draft as drafts
from host import pdf_import
from host.contracts import Field, Refused


class Pdf:
    id = "pdf"
    label = "PDF file"

    def matches(self, req) -> bool:
        if req.get("kind") != "upload":
            return False
        data = req.get("data") or b""
        if data[:5] == b"%PDF-":
            return True
        return (req.get("filename") or "").lower().endswith(".pdf")

    def read(self, req, fetch, cred=None):
        data = req.get("data") or b""
        if not data:
            raise Refused("no file", "the upload arrived empty", status=400)
        if data[:5] != b"%PDF-":
            raise Refused("not a pdf", "this file does not start with a PDF header")
        parsed = pdf_import.extract(data)
        if parsed.get("scanned"):
            raise Refused(
                "scanned pdf",
                "this PDF has no text layer: %d pages, no text found. It needs "
                "OCR before it can be read aloud." % parsed.get("pages", 0),
                scanned=True, pages=parsed.get("pages", 0))
        book = drafts.draft_to_book(parsed, self.id)
        book.meta["draft"] = parsed
        return book

    def same(self, stored, incoming) -> bool:
        a = (stored or {}).get("identity") or ""
        b = (incoming or {}).get("identity") or ""
        return bool(a and a == b)

    def settings(self):
        return []

    def credential_hosts(self):
        return []
