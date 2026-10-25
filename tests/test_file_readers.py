"""PDF and DOCX as real readers: matching, refusal, and the preview route.

Three things worth pinning, because each one is a place where being friendly
would cost somebody money:

1. Matching by content, not extension. A docx is a zip and so is an epub, so
   "kind == upload" cannot be the test any more.
2. A scanned PDF is refused with a reason and a page count, never imported as
   an empty book. OCR is a separate paid activity.
3. The preview writes nothing to the shelf and states the character count that
   will be charged, so the number appears before the charge, not after.
"""

import base64
import io
import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pypdf

from host import draft as drafts
from host import docx_import
from host import pdf_import
from host.app import App, make_handler
from host.contracts import Refused
from tests.test_app import SPEECH_BODY, STORAGE_BODY
from tests.test_pluginhost import write_plugin


# ---- building test files without adding a PDF library -------------------------

def make_pdf(pages) -> bytes:
    """A valid PDF with a text layer. pages = [[(size, x, y, text), ...], ...].

    Hand-built on purpose: the test suite should not need reportlab to prove
    that the reader reads a PDF, and a generator that decides its own layout
    would hide the layout facts the reader depends on.
    """
    objs = {}
    n = len(pages)
    page_ids = [3 + i for i in range(n)]
    content_ids = [3 + n + i for i in range(n)]
    font_id = 3 + 2 * n
    objs[1] = "<</Type/Catalog/Pages 2 0 R>>"
    objs[2] = "<</Type/Pages/Kids[%s]/Count %d>>" % (
        " ".join("%d 0 R" % i for i in page_ids), n)
    for i, lines in enumerate(pages):
        body = []
        for size, x, y, text in lines:
            esc = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
            body.append("BT /F1 %g Tf %g %g Td (%s) Tj ET" % (size, x, y, esc))
        stream = "\n".join(body)
        objs[content_ids[i]] = "<</Length %d>>stream\n%s\nendstream" % (len(stream), stream)
        objs[page_ids[i]] = ("<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]"
                             "/Resources<</Font<</F1 %d 0 R>>>>/Contents %d 0 R>>"
                             % (font_id, content_ids[i]))
    objs[font_id] = "<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>"
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = {}
    for num in sorted(objs):
        offsets[num] = out.tell()
        out.write(("%d 0 obj\n%s\nendobj\n" % (num, objs[num])).encode("latin-1"))
    xref = out.tell()
    top = max(objs) + 1
    out.write(("xref\n0 %d\n" % top).encode())
    out.write(b"0000000000 65535 f \n")
    for num in range(1, top):
        out.write(("%010d 00000 n \n" % offsets[num]).encode())
    out.write(("trailer\n<</Size %d/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF\n"
               % (top, xref)).encode())
    return out.getvalue()


TEXT_PDF = make_pdf([
    [(20, 72, 700, "Chapter One"),
     (11, 72, 660, "It was a bright cold day in April and the clocks were striking thirteen."),
     (11, 72, 646, "The hallway smelt of boiled cabbage and old rag mats.")],
    [(20, 72, 700, "Chapter Two"),
     (11, 72, 660, "She walked to the window and looked out at the street below.")],
])


def blank_pdf(pages: int = 3) -> bytes:
    w = pypdf.PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def make_docx(paragraphs) -> bytes:
    """A minimal .docx: paragraphs = [(style, text)], style "" for body text."""
    import zipfile
    from xml.sax.saxutils import escape

    body = []
    for style, text in paragraphs:
        ppr = ""
        if style:
            ppr = '<w:pPr><w:pStyle w:val="%s"/></w:pPr>' % style
        body.append("<w:p>%s<w:r><w:t xml:space=\"preserve\">%s</w:t></w:r></w:p>"
                    % (ppr, escape(text)))
    doc = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           '<w:body>%s</w:body></w:document>' % "".join(body))
    core = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"'
            ' xmlns:dc="http://purl.org/dc/elements/1.1/">'
            '<dc:title>A Test Book</dc:title><dc:creator>Nobody</dc:creator>'
            '</cp:coreProperties>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')
        z.writestr("word/document.xml", doc)
        z.writestr("docProps/core.xml", core)
    return buf.getvalue()


# ---- the plugins, loaded the way the host loads them -------------------------

def _load(name):
    import importlib.util
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "plugins", "reader", name, "plugin.py")
    spec = importlib.util.spec_from_file_location("ren_plugin_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return getattr(mod, name.capitalize())()


class TestMatchingByContent(unittest.TestCase):
    def setUp(self):
        self.pdf = _load("pdf")
        self.docx = _load("docx")
        self.epub = _load("epub")

    def test_pdf_claims_its_own_bytes(self):
        self.assertTrue(self.pdf.matches({"kind": "upload", "data": TEXT_PDF}))

    def test_pdf_does_not_claim_a_zip(self):
        self.assertFalse(self.pdf.matches({"kind": "upload", "data": make_docx([("", "x")])}))

    def test_docx_claims_a_zip_with_a_word_body(self):
        data = make_docx([("", "x")])
        self.assertTrue(self.docx.matches({"kind": "upload", "data": data}))
        self.assertFalse(self.pdf.matches({"kind": "upload", "data": data}))

    def test_epub_stops_claiming_every_zip(self):
        # The bug this fixes: epub matched kind == "upload", so a docx went to
        # the epub parser and failed as an epub instead of reading as a docx.
        self.assertFalse(self.epub.matches({"kind": "upload",
                                            "data": make_docx([("", "x")])}))
        self.assertFalse(self.epub.matches({"kind": "upload", "data": TEXT_PDF}))

    def test_epub_still_claims_a_real_container(self):
        import zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("mimetype", "application/epub+zip")
            z.writestr("META-INF/container.xml", "<container/>")
        self.assertTrue(self.epub.matches({"kind": "upload", "data": buf.getvalue()}))

    def test_a_url_request_goes_to_no_file_reader(self):
        for p in (self.pdf, self.docx):
            self.assertFalse(p.matches({"kind": "url", "url": "https://x/y"}))


class TestRefusals(unittest.TestCase):
    def test_scanned_pdf_is_refused_with_pages_and_ocr_named(self):
        p = _load("pdf")
        with self.assertRaises(Refused) as cm:
            p.read({"kind": "upload", "data": blank_pdf(3)}, None)
        e = cm.exception
        self.assertEqual(e.status, 422)
        self.assertEqual(e.error, "scanned pdf")
        self.assertEqual(e.extra.get("pages"), 3)
        self.assertIn("OCR", e.detail)
        self.assertIn("3 pages", e.detail)

    def test_a_pdf_that_is_not_a_pdf_is_refused_as_a_bad_file(self):
        p = _load("pdf")
        with self.assertRaises(Refused) as cm:
            p.read({"kind": "upload", "data": b"not a pdf at all",
                    "filename": "x.pdf"}, None)
        self.assertEqual(cm.exception.error, "not a pdf")

    def test_empty_docx_is_refused_not_imported_empty(self):
        d = _load("docx")
        with self.assertRaises(Refused) as cm:
            d.read({"kind": "upload", "data": make_docx([("", "   ")])}, None)
        self.assertEqual(cm.exception.error, "empty document")


class TestDraftToBook(unittest.TestCase):
    def test_blocks_become_paragraphs_and_empty_chapters_go(self):
        parsed = {"title": "T", "identity": "pdf:x", "author": "",
                  "chapters": [{"title": "One", "blocks": [{"id": "c0b0", "text": "a"},
                                                           {"id": "c0b1", "text": "b"}]},
                               {"title": "Empty", "blocks": []}],
                  "flags": [], "words": 2}
        book = drafts.draft_to_book(parsed, "pdf")
        self.assertEqual(book.title, "T")
        self.assertEqual([c.title for c in book.chapters], ["One"])
        self.assertEqual(book.chapters[0].text, "a\n\nb")
        self.assertEqual(book.meta["identity"], "pdf:x")

    def test_the_summary_counts_the_characters_that_will_be_charged(self):
        parsed = pdf_import.extract(TEXT_PDF)
        s = drafts.draft_summary(parsed, "pdf")
        self.assertEqual(s["chars"], sum(c["chars"] for c in s["chapters"]))
        self.assertEqual(len(s["chapters"]), 2)
        self.assertEqual(s["chapters"][0]["title"], "Chapter One")
        # flagged-but-kept text counts: the preview describes what it imports
        self.assertGreaterEqual(s["chars"], s["words"])

    def test_a_flag_points_at_a_block_that_exists(self):
        parsed = docx_import.extract(make_docx([("Heading1", "One"),
                                                ("", "12"),
                                                ("", "The body text of the chapter.")]))
        ids = {b["id"] for ch in parsed["chapters"] for b in ch["blocks"]}
        for f in parsed["flags"]:
            self.assertIn(f["block"], ids)
            self.assertIn(f["rule"], ("toc", "repeated-header", "page-number",
                                      "all-caps", "fragment", "front-matter"))


class TestPreviewRoute(unittest.TestCase):
    """The preview is a read, not a write: the shelf must be identical after."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fireside-prev-")
        root = os.path.join(self.tmp, "plugins")
        rroot = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for name in ("pdf", "docx", "epub"):
            shutil.copytree(os.path.join(rroot, "plugins", "reader", name),
                            os.path.join(root, "reader", name))
        write_plugin(root, "speech", "fake", body=SPEECH_BODY)
        write_plugin(root, "storage", "mem", body=STORAGE_BODY)
        self.app = App(root, db_path=os.path.join(self.tmp, "db.sqlite"), storage_id="mem")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _post(self, path, obj):
        req = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.port, path),
            data=json.dumps(obj).encode(), headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def _preview(self, data, filename):
        return self._post("/import/preview", {"user": "u1", "kind": "upload",
                                              "filename": filename,
                                              "data_b64": base64.b64encode(data).decode()})

    def _shelf(self):
        with urllib.request.urlopen(
                "http://127.0.0.1:%d/library?user=u1" % self.port) as r:
            return json.loads(r.read())

    def test_a_text_pdf_previews_with_chapters_and_a_character_count(self):
        s, out = self._preview(TEXT_PDF, "book.pdf")
        self.assertEqual(s, 200, out)
        p = out["preview"]
        self.assertEqual(p["source"], "pdf")
        self.assertEqual(len(p["chapters"]), 2)
        self.assertGreater(p["chars"], 50)
        self.assertEqual(self._shelf(), [])

    def test_a_scanned_pdf_previews_as_a_422_with_a_reason(self):
        s, out = self._preview(blank_pdf(2), "scan.pdf")
        self.assertEqual(s, 422)
        self.assertEqual(out["error"], "scanned pdf")
        self.assertEqual(out["pages"], 2)

    def test_a_bad_file_leaves_nothing_behind(self):
        s, out = self._preview(b"PK\x03\x04junk", "broken.docx")
        self.assertEqual(s, 400, out)
        self.assertEqual(out["error"], "could not read this file")
        self.assertEqual(self._shelf(), [])

    def test_a_docx_previews_from_its_heading_styles(self):
        data = make_docx([("Heading1", "One"), ("", "The first line of the book."),
                          ("Heading1", "Two"), ("", "The second chapter here.")])
        s, out = self._preview(data, "book.docx")
        self.assertEqual(s, 200, out)
        p = out["preview"]
        self.assertEqual(p["source"], "docx")
        self.assertEqual([c["title"] for c in p["chapters"]], ["One", "Two"])
        self.assertEqual(p["title"], "A Test Book")
        self.assertEqual(p["author"], "Nobody")

    def test_importing_the_same_pdf_twice_is_still_a_question(self):
        b = base64.b64encode(TEXT_PDF).decode()
        s, first = self._post("/import", {"user": "u1", "kind": "upload",
                                          "filename": "book.pdf", "data_b64": b})
        self.assertEqual(s, 200, first)
        s, again = self._post("/import", {"user": "u1", "kind": "upload",
                                          "filename": "renamed.pdf", "data_b64": b})
        self.assertEqual(s, 409, again)
        s, prev = self._preview(TEXT_PDF, "book.pdf")
        self.assertEqual(s, 200)
        self.assertIn("existing", prev["preview"])


if __name__ == "__main__":
    unittest.main()
