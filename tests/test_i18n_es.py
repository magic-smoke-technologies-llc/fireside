"""Spanish is a language we ship, not a directory we added.

English was the only catalog, so negotiation, the picker and the injection all
worked and the site was still English. These answer the question a user would
ask: does ?lang=es actually give me Spanish, all of it, and does the one phrase
I have to type still work when I am reading Spanish?

Everything lives on a unittest.TestCase on purpose - CI runs unittest discover,
so a bare `def test_x()` is collected by nothing and reports nothing.
"""
import json
import os
import pathlib
import re
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

from host import i18n

ROOT = pathlib.Path(__file__).resolve().parents[1]
LOCALE = ROOT / "ui" / "i18n"

# Identical in both languages on purpose: a file extension, an acronym, a name.
SAME_ON_PURPOSE = {".epub", "ISBN", "fireside", "https://…"}


def _load(code):
    return json.loads((LOCALE / (code + ".json")).read_text(encoding="utf-8"))


class TestSpanishCatalog(unittest.TestCase):

    def test_spanish_ships_and_is_offered(self):
        codes = [l["code"] for l in i18n.available()]
        assert "es" in codes
        assert codes[0] == "en"  # English is still the source language
        assert _load("es")["name"] == "Español"
        assert _load("es")["dir"] == "ltr"

    def test_spanish_covers_every_english_string(self):
        en = _load("en")["messages"]
        es = _load("es")["messages"]
        missing = sorted(k for k in en if k not in es)
        extra = sorted(k for k in es if k not in en)
        assert not missing, "untranslated: %r" % missing[:10]
        assert not extra, "no English source for: %r" % extra[:10]

    def test_no_entry_is_english_wearing_a_spanish_catalog(self):
        # A catalog full of copies passes a "does it exist" test and reads English.
        en = _load("en")["messages"]
        es = _load("es")["messages"]
        copied = sorted(k for k, v in es.items()
                        if v == en[k] and k not in SAME_ON_PURPOSE)
        assert not copied, "not translated: %r" % copied[:10]

    def test_no_translation_is_empty_or_drops_a_placeholder(self):
        en = _load("en")["messages"]
        es = _load("es")["messages"]
        for k, v in es.items():
            assert v.strip(), "empty translation for %r" % k
            for token in [t for t in ("{n}", "{count}") if t in en[k]]:
                assert token in v, "lost %s in %r" % (token, k)

    def test_spanish_is_negotiated(self):
        codes = [l["code"] for l in i18n.available()]
        assert i18n.negotiate("es", None, None, codes) == "es"
        assert i18n.negotiate("es-MX", None, None, codes) == "es"
        assert i18n.negotiate(None, "es", "en-US,en;q=0.9", codes) == "es"
        assert i18n.negotiate(None, None, "es-ES,es;q=0.9,en;q=0.8", codes) == "es"

    def test_the_typed_delete_phrase_works_in_spanish(self):
        # The placeholder is translated, so the phrase a Spanish reader types
        # has to be accepted or the one destructive button breaks for them.
        from host.app import _norm_confirm, DELETE_PHRASES
        assert _norm_confirm("  Delete My Account ") in DELETE_PHRASES
        assert _norm_confirm("Eliminar mi Cuenta") in DELETE_PHRASES
        assert _norm_confirm("ELIMINAR MI CUENTA") in DELETE_PHRASES
        assert _norm_confirm("Eliminar mi cuenta, por favor") not in DELETE_PHRASES
        assert _norm_confirm("") not in DELETE_PHRASES
        assert _norm_confirm(None) not in DELETE_PHRASES


class TestSpanishPage(unittest.TestCase):
    """The served page, in Spanish, from the real host."""

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

    def get(self, path):
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path))
        with urllib.request.urlopen(req) as r:
            return r.status, r.read().decode("utf-8", "replace")

    def test_query_language_renders_the_page_in_spanish(self):
        status, body = self.get("/?lang=es")
        assert status == 200
        assert '<html lang="es" dir="ltr"' in body
        # The markup itself is Spanish, not English waiting for the script:
        # asserting "the catalog contains Español" would pass while a Spanish
        # reader still reads English navigation until JS runs.
        assert 'data-t="Sign in">Entrar<' in body
        assert 'data-t="Library">Biblioteca<' in body
        assert 'data-t="Sign in">Sign in<' not in body
        assert '"Español"' in body

    def test_english_markup_is_untouched(self):
        status, body = self.get("/")
        assert status == 200
        assert 'data-t="Sign in">Sign in<' in body

    def test_a_tooltip_is_translated_too(self):
        status, body = self.get("/?lang=es")
        assert status == 200
        # `title="Capítulo anterior"` alone passed while the English title was
        # still on the tag in front of it: two identical attributes, and a
        # browser keeps the first. So the tag is checked, not the substring.
        tag = re.search(r'<[^>]*data-t-title="Previous chapter"[^>]*>', body)
        assert tag, "no marked tag for the previous-chapter tooltip"
        titles = re.findall(r'(?<!-)\btitle="([^"]*)"', tag.group(0))
        assert titles == ["Capítulo anterior"], titles

    def test_no_tag_has_two_of_the_same_attribute(self):
        # A duplicate attribute is invalid HTML and the browser silently keeps
        # the first, which is how a translated aria-label ships as English.
        for path in ("/", "/?lang=es"):
            status, body = self.get(path)
            assert status == 200
            for m in re.finditer(r"<[a-zA-Z][^>]*>", body):
                names = re.findall(r"(?<=\s)([a-zA-Z-]+)=", m.group(0))
                assert len(names) == len(set(names)), (path, m.group(0)[:120])

    def test_english_is_still_the_default(self):
        status, body = self.get("/")
        assert status == 200
        assert '<html lang="en" dir="ltr"' in body
