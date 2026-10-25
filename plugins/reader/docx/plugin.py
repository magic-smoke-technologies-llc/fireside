"""v2 reader plugin: docx. The parse is host/docx_import.py; this is the contract.

Order 7, between pdf (6) and epub (10). Both docx and epub are zip files, so
matching is by content, not by extension: a docx is a zip whose central
directory names word/document.xml. The epub reader asks the same question in the
opposite direction, so whichever one is true claims the upload.
"""
import io
import os
import sys
import zipfile

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from host import docx_import
from host import draft as drafts
from host.contracts import Field, Refused


def _names(data: bytes) -> list:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            return z.namelist()
    except (zipfile.BadZipFile, OSError):
        return []


class Docx:
    id = "docx"
    label = "Word document"

    def matches(self, req) -> bool:
        if req.get("kind") != "upload":
            return False
        data = req.get("data") or b""
        if data[:4] != b"PK\x03\x04":
            return False
        return "word/document.xml" in _names(data)

    def read(self, req, fetch, cred=None):
        data = req.get("data") or b""
        if not data:
            raise Refused("no file", "the upload arrived empty", status=400)
        if "word/document.xml" not in _names(data):
            raise Refused("not a docx",
                          "this file has no word/document.xml, so it is not a Word document")
        parsed = docx_import.extract(data)
        if not (parsed.get("words") or 0):
            raise Refused("empty document",
                          "there is no text in this document's body")
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
