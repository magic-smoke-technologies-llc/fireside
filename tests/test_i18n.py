"""The website's own words are translatable; a book's are not touched.

What this must prove, in order of how it can break:

1. negotiation picks the right language from ?lang=, the cookie, then
   Accept-Language, and never hands back a language it does not have;
2. the page is rendered in that language before first paint - the lang
   attribute, the direction, and the catalog all arrive in the HTML;
3. every string marked data-t in ui/index.html has an entry in the English
   catalog, and the extractor has no leftovers (so a new hardcoded sentence
   shows up as a failing test instead of a string nobody can translate);
4. /i18n.js is served and is public - the sign-in page needs it before anyone
   has a session.

Book text, reader plugins and TTS are deliberately out of scope: "translate the
website" is not "translate the library".
"""
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

from host import i18n

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _codes():
    return [l["code"] for l in i18n.available()]


class TestCatalogAndNegotiation(unittest.TestCase):
    """Module-level `def test_x()` is invisible to unittest discover, and CI
    runs unittest: the tests reported green while never being collected. These
    live on a TestCase so the number CI reports is the number that ran."""

    def test_english_ships_and_is_first(self):
        codes = _codes()
        assert "en" in codes
        assert codes[0] == "en"


    def test_query_beats_cookie_beats_header(self):
        codes = ["en", "es", "de"]
        assert i18n.negotiate("de", "es", "en-US,en;q=0.9", codes) == "de"
        assert i18n.negotiate(None, "es", "de-DE,de;q=0.9", codes) == "es"
        assert i18n.negotiate(None, None, "de-DE,de;q=0.9", codes) == "de"


    def test_accept_language_respects_q_and_falls_back(self):
        codes = ["en", "es", "de"]
        assert i18n.negotiate(None, None, "de;q=0.4, es;q=0.8", codes) == "es"
        assert i18n.negotiate(None, None, "ja,ko;q=0.8", codes) == "en"
        assert i18n.negotiate(None, None, "", codes) == "en"


    def test_a_language_we_do_not_have_is_not_invented(self):
        codes = ["en", "es"]
        assert i18n.negotiate("fr", "fr", "fr-FR,fr;q=0.9", codes) == "en"


    def test_a_region_picks_its_base_language(self):
        codes = ["en", "es"]
        assert i18n.negotiate("es-MX", None, None, codes) == "es"


    def test_junk_in_the_cookie_is_ignored(self):
        assert i18n.negotiate('<script>', '";drop', None, ["en"]) == "en"


    def test_page_carries_language_direction_and_catalog(self):
        page = '<!doctype html><html lang="en"><head><title>x</title></head><body></body></html>'
        out = i18n.inject(page, code="en")
        assert '<html lang="en" dir="ltr"' in out
        assert "window.__FS_I18N__" in out
        assert "/i18n.js" in out
        # the catalog goes in before </head>, so the app script cannot beat it
        assert out.index("__FS_I18N__") < out.index("</head>")


    def test_catalog_cannot_break_out_of_its_script_tag(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "en.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"name": "English", "messages": {"x": "</script><b>hi</b>"}}, f)
            old = i18n.LOCALE_DIR
            i18n.LOCALE_DIR = tmp
            i18n._cache.clear()
            try:
                out = i18n.inject("<html lang=\"en\"><head></head></html>", code="en")
            finally:
                i18n.LOCALE_DIR = old
                i18n._cache.clear()
        assert "</script><b>hi</b>" not in out
        assert "<\\/script>" in out


    def test_rtl_language_sets_direction(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "ar.json"), "w", encoding="utf-8") as f:
                json.dump({"name": "العربية", "messages": {}}, f)
            old = i18n.LOCALE_DIR
            i18n.LOCALE_DIR = tmp
            i18n._cache.clear()
            try:
                cat = i18n.catalog("ar")
            finally:
                i18n.LOCALE_DIR = old
                i18n._cache.clear()
        assert cat["dir"] == "rtl"


    def test_every_marked_string_has_a_catalog_entry(self):
        # Ask the extractor what counts as a string: a regex over the raw file sees
        # data-t inside comments and CSS and calls it a key.
        sys.path.insert(0, str(ROOT / "tools"))
        import i18n_extract
        _src, f = i18n_extract.analyse()
        cat = json.loads((ROOT / "ui" / "i18n" / "en.json").read_text(encoding="utf-8"))
        messages = cat["messages"]
        keys = set(f.marked) | {v for _name, v in f.attrs}
        missing = sorted(k for k in keys if k not in messages)
        assert not missing, "no catalog entry for: %r" % missing[:10]


    def test_no_hardcoded_english_left_in_the_static_page(self):
        # the extractor is the authority on what "translatable" means: if it finds
        # a string it cannot reach, a translator cannot either.
        out = subprocess.run([sys.executable, str(ROOT / "tools" / "i18n_extract.py"), "--check"],
                             capture_output=True, text=True)
        assert out.returncode == 0, out.stdout + out.stderr

class TestServedPage(unittest.TestCase):
    """Boot the real host and read the served page."""

    def setUp(self):
        from host.app import App, make_handler
        from tests.test_app import READER_BODY, SPEECH_BODY, STORAGE_BODY
        from tests.test_pluginhost import write_plugin
        self.tmp = tempfile.mkdtemp()
        root = os.path.join(self.tmp, "plugins")
        write_plugin(root, "reader", "fake", order=10, body=READER_BODY)
        write_plugin(root, "speech", "fake", body=SPEECH_BODY)
        write_plugin(root, "storage", "mem", body=STORAGE_BODY)
        app = App(root, db_path=os.path.join(self.tmp, "db.sqlite"),
                  storage_id="mem", hosted=False, require_auth=False)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def get(self, path, headers=None):
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path),
                                     headers=headers or {})
        with urllib.request.urlopen(req) as r:
            return r.status, r.read().decode("utf-8", "replace"), dict(r.headers)

    def test_page_injects_catalog_and_language(self):
        status, body, _h = self.get("/")
        assert status == 200
        assert 'lang="en"' in body
        assert "window.__FS_I18N__" in body
        assert "/i18n.js" in body
        # the catalog is the one the page's own strings are keyed by
        assert '"Sign in"' in body or "Sign in" in body

    def test_language_cookie_is_honoured(self):
        status, body, _h = self.get("/", {"Cookie": "fs_lang=en"})
        assert status == 200 and 'lang="en"' in body

    def test_query_language_is_honoured(self):
        status, body, _h = self.get("/?lang=en&cb=1")
        assert status == 200 and 'lang="en"' in body

    def test_i18n_js_is_public_and_javascript(self):
        status, body, headers = self.get("/i18n.js")
        assert status == 200
        assert "javascript" in headers.get("Content-Type", "")
        assert "FS_I18N" in body


class TestExtractorSafety(unittest.TestCase):
    """The marking pass is a regex over the page; code must survive it."""

    def test_the_extractor_never_marks_code_as_copy(self):
        """The marking pass must leave <script> and <style> blocks byte-identical.

        It ran once and marked the native-bridge <script> with a data-t attribute
        carrying its own escaped source - the blank page of #173/#175, produced by
        the tool meant to prevent it. Two runs must also be one run: repeated
        copies of data-t-title on one button were the residue of that.
        """
        import shutil
        sys.path.insert(0, str(ROOT / "tools"))
        import i18n_extract

        src = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            page = os.path.join(tmp, "index.html")
            cat = os.path.join(tmp, "en.json")
            shutil.copy(str(ROOT / "ui" / "index.html"), page)
            shutil.copy(str(ROOT / "ui" / "i18n" / "en.json"), cat)
            old = (i18n_extract.PAGE, i18n_extract.CATALOG)
            i18n_extract.PAGE, i18n_extract.CATALOG = page, cat
            try:
                i18n_extract.write()
                once = open(page, encoding="utf-8").read()
                i18n_extract.write()
                twice = open(page, encoding="utf-8").read()
            finally:
                i18n_extract.PAGE, i18n_extract.CATALOG = old

        assert once == twice, "the marking pass is not idempotent"
        for block in re.findall(r"<(?:script|style)\b.*?</(?:script|style)>", src, re.S):
            assert block in once, "the extractor rewrote a code block: %r" % block[:80]
        # a host placeholder is filled in by the server and is not a sentence
        assert 'data-t="v__HOST_VERSION__"' not in once
