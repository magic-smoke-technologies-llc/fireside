"""Sources: parsing AO3 and a web page, and the host-owned fetch they call.

These are unit tests with fixture HTML in the file. The two self-check scripts
next door (test_sources.py, test_providers.py) boot a real server; this one
never opens a socket except to a loopback server it starts itself.
"""

import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from host import egress as _fetch, egress_backends as _backends
from plugins.reader.ao3 import plugin as ao3
from plugins.reader.web import plugin as web
from sources import describe, ingest, request_key
from plugins.reader.epub.plugin import Epub


AO3_PAGE = """
<html><body>
<h2 class="title">A Work</h2>
<div id="workskin">
  <div id="chapter-1">
    <h3 class="landmark heading" id="work">Chapter Text</h3>
    <h3 class="title">First Light</h3>
    <div class="userstuff"><p>%s</p></div>
  </div>
  <div id="chapter-2">
    <h3 class="landmark heading" id="work">Chapter Text</h3>
    <h3 class="title">Second Wind</h3>
    <div class="userstuff"><p>%s</p></div>
  </div>
</div></body></html>
""" % ("The coil hummed. " * 12, "The field reversed. " * 12)

AO3_SINGLE = """
<html><body><h2 class="title">One Shot</h2>
<div id="workskin"><div class="userstuff"><p>%s</p></div></div>
</body></html>
""" % ("Only one chapter here. " * 10)

AO3_LOGIN = '<html><body><form id="login"><input name="user[login]"></form></body></html>'
AO3_ADULT = '<html><body><div id="adult">This work could have adult content</div>' \
            '<a href="?view_adult=true">Proceed</a></body></html>'
AO3_EMPTY = '<html><body><div id="workskin"></div></body></html>'


class TestAo3Parse(unittest.TestCase):
    def test_multi_chapter(self):
        b = ao3.parse_ao3(AO3_PAGE, "https://archiveofourown.org/works/1")
        self.assertEqual(b["title"], "A Work")
        self.assertEqual(len(b["chapters"]), 2)
        self.assertEqual(b["chapters"][0]["title"], "First Light")
        self.assertNotIn("Chapter Text", b["chapters"][0]["text"])
        self.assertIn("coil hummed", b["chapters"][0]["text"])

    def test_single_chapter_fallback(self):
        b = ao3.parse_ao3(AO3_SINGLE, "https://archiveofourown.org/works/2")
        self.assertEqual(len(b["chapters"]), 1)
        self.assertEqual(b["chapters"][0]["title"], "One Shot")

    def test_login_wall_says_what_to_do(self):
        with self.assertRaises(ValueError) as c:
            ao3.parse_ao3(AO3_LOGIN, "https://archiveofourown.org/works/3")
        self.assertIn("session cookie", str(c.exception))

    def test_adult_gate(self):
        with self.assertRaises(ValueError) as c:
            ao3.parse_ao3(AO3_ADULT, "https://archiveofourown.org/works/4")
        self.assertIn("adult gate", str(c.exception))

    def test_no_body(self):
        with self.assertRaises(ValueError):
            ao3.parse_ao3(AO3_EMPTY, "https://archiveofourown.org/works/5")

    def test_url_matching(self):
        self.assertTrue(ao3.is_work_url("https://archiveofourown.org/works/39212511"))
        self.assertTrue(ao3.is_work_url("https://www.archiveofourown.org/works/1/chapters/2"))
        self.assertFalse(ao3.is_work_url("https://archiveofourown.org/tags/Fire"))
        self.assertFalse(ao3.is_work_url("https://notarchiveofourown.org/works/1"))

    def test_full_work_url_drops_and_adds_flags(self):
        u = ao3.full_work_url("https://archiveofourown.org/works/1?view_adult=false&x=1")
        self.assertIn("view_adult=true", u)
        self.assertIn("view_full_work=true", u)
        self.assertNotIn("view_adult=false", u)
        self.assertIn("x=1", u)

    def test_matches_and_meta(self):
        s = ao3.Ao3()
        self.assertTrue(s.matches({"kind": "url",
                                   "url": "https://archiveofourown.org/works/1"}))
        self.assertFalse(s.matches({"kind": "upload"}))
        book = s.read({"url": "https://archiveofourown.org/works/1"},
                      lambda url, cred=None, credential_hosts=(): AO3_PAGE)
        self.assertEqual(book.meta["source"], "ao3")
        self.assertEqual(s.credential_hosts(), ["archiveofourown.org"])
        self.assertEqual(s.transport()["backend"], "browser")
        self.assertEqual([f.name for f in s.settings()], ["cookie"])


WEB_PAGE = """<html><head><title>  A Page Title </title></head><body>
<h1>Heading</h1><article><p>%s</p></article></body></html>""" % ("real words here " * 20)
WEB_MAIN = "<html><body><main><p>%s</p></main></body></html>" % ("main text " * 30)
WEB_BARE = "<html><body><p>%s</p></body></html>" % ("bare body text " * 30)
WEB_THIN = "<html><body><article><p>too short</p></article></body></html>"

# Wordpress with a related-posts plugin: the real story sits in .entry-content,
# then six tiny <article> widgets and a comment thread follow it. Taking the
# first <article> on the page is how a 20k-word chapter read as "no readable
# text" (quantumbang.org, 2026-10-23).
_WP_STORY = "story words that must be read aloud " * 12
_WP_RELATED = """<html><head><title>A Part</title></head><body>
<div class="entry-content">
  <div class="form-custom-fields-container above-content"></div>
  <div class="inner"><p>%s</p></div>
</div>
<article class="post related"><h3><a href="/x">Related one</a></h3></article>
<article class="post related"><h3><a href="/y">Related two</a></h3></article>
<article class="post related"><h3><a href="/z">Related three</a></h3></article>
<div id="comments"><h3>4 Comments:</h3><div class="comment-body"><p>Bravo!</p></div></div>
<div id="respond"><h3>Leave a Reply</h3></div>
</body></html>""" % _WP_STORY


class TestWebParse(unittest.TestCase):
    def test_article_and_title(self):
        b = web.parse_web(WEB_PAGE, "https://example.com/a")
        self.assertEqual(b["title"], "A Page Title")
        self.assertEqual(b["chapters"][0]["title"], "Heading")
        self.assertIn("real words", b["chapters"][0]["text"])

    def test_main_and_body_fallbacks(self):
        self.assertIn("main text", web.parse_web(WEB_MAIN, "https://e.com")["chapters"][0]["text"])
        self.assertIn("bare body", web.parse_web(WEB_BARE, "https://e.com")["chapters"][0]["text"])

    def test_biggest_container_wins_not_the_first_article(self):
        b = web.parse_web(_WP_RELATED, "https://quantumbang.org/a/")
        text = b["chapters"][0]["text"]
        self.assertIn("must be read aloud", text)
        for junk in ("Related one", "Related two", "Bravo!", "Leave a Reply", "4 Comments"):
            self.assertNotIn(junk, text)

    def test_nested_divs_do_not_truncate_the_container(self):
        text = web.parse_web(_WP_RELATED, "https://e.com")["chapters"][0]["text"]
        self.assertEqual(text.count("must be read aloud"), _WP_STORY.count("must be read aloud"))

    def test_thin_page_is_refused(self):
        with self.assertRaises(ValueError):
            web.parse_web(WEB_THIN, "https://e.com")

    def test_web_matches_http_only(self):
        s = web.Web()
        self.assertTrue(s.matches({"kind": "url", "url": "http://e.com"}))
        self.assertFalse(s.matches({"kind": "url", "url": "ftp://e.com"}))
        self.assertFalse(s.matches({"kind": "upload"}))
        book = s.read({"url": "https://e.com"}, lambda url, cred=None, credential_hosts=(): WEB_PAGE)
        self.assertEqual(book.meta["url"], "https://e.com")
        self.assertEqual(s.settings(), [])
        self.assertEqual(s.credential_hosts(), [])


class TestFetchScope(unittest.TestCase):
    def test_host_allowed(self):
        self.assertTrue(_fetch.host_allowed("https://archiveofourown.org/x",
                                           ["archiveofourown.org"]))
        self.assertTrue(_fetch.host_allowed("https://www.archiveofourown.org/x",
                                           [".archiveofourown.org"]))
        self.assertFalse(_fetch.host_allowed("https://notarchiveofourown.org/x",
                                             ["archiveofourown.org"]))
        self.assertFalse(_fetch.host_allowed("https://e.com/x", []))

    def test_bad_scheme_and_backend(self):
        with self.assertRaises(ValueError):
            _fetch.fetch("ftp://e.com/x")
        with self.assertRaises(ValueError):
            _fetch.fetch("https://e.com/x", transport={"backend": "nope"})

    def test_cookie_goes_only_to_declared_hosts(self):
        seen = {}

        def fake(url, headers, limit, timeout, opts):
            seen["cookie"] = headers.get("Cookie")
            seen["ua"] = headers.get("User-Agent")
            return "ok"

        saved = _backends.BACKENDS["plain"]
        _backends.BACKENDS["plain"] = fake
        try:
            _fetch.fetch("https://archiveofourown.org/w/1", cred={"cookie": "sessionid=x"},
                         credential_hosts=["archiveofourown.org"])
            self.assertEqual(seen["cookie"], "sessionid=x")
            self.assertIn("reader-local", seen["ua"])
            _fetch.fetch("https://evil.example/w/1", cred={"cookie": "sessionid=x"},
                         credential_hosts=["archiveofourown.org"])
            self.assertIsNone(seen["cookie"])
        finally:
            _backends.BACKENDS["plain"] = saved

    def test_session_key_is_a_cookie_too(self):
        seen = {}

        def fake(url, headers, limit, timeout, opts):
            seen["cookie"] = headers.get("Cookie")
            return "ok"

        saved = _backends.BACKENDS["plain"]
        _backends.BACKENDS["plain"] = fake
        try:
            _fetch.fetch("https://archiveofourown.org/w/1", cred={"session": "s=1"},
                         credential_hosts=["archiveofourown.org"])
            self.assertEqual(seen["cookie"], "s=1")
        finally:
            _backends.BACKENDS["plain"] = saved

    def test_transport_headers_and_retries(self):
        calls = {"n": 0}

        def flaky(url, headers, limit, timeout, opts):
            calls["n"] += 1
            if calls["n"] < 2:
                raise RuntimeError("525")
            return headers.get("X-Test", "") + "done"

        saved_b, saved_sleep = _backends.BACKENDS["plain"], _fetch.time.sleep
        _backends.BACKENDS["plain"] = flaky
        _fetch.time.sleep = lambda s: None
        try:
            out = _fetch.fetch("https://e.com/x",
                               transport={"headers": {"X-Test": "h:"}, "retries": 2,
                                          "backoff": 0})
            self.assertEqual(out, "h:done")
            self.assertEqual(calls["n"], 2)
            _backends.BACKENDS["plain"] = lambda *a: (_ for _ in ()).throw(RuntimeError("down"))
            with self.assertRaises(RuntimeError):
                _fetch.fetch("https://e.com/x", transport={"retries": 1})
        finally:
            _backends.BACKENDS["plain"] = saved_b
            _fetch.time.sleep = saved_sleep

    def test_bind_transport_passthrough(self):
        def base(url, cred=None, credential_hosts=()):
            return "base"

        self.assertIs(_fetch.bind_transport(base, None), base)
        bound = _fetch.bind_transport(
            lambda url, cred=None, credential_hosts=(), transport=None: transport["x"],
            {"x": "carried"})
        self.assertEqual(bound("https://e.com"), "carried")


class TestPlainBackend(unittest.TestCase):
    def setUp(self):
        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                body = "<html><body>hi</body></html>".encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d/" % self.httpd.server_address[1]

    def tearDown(self):
        self.httpd.shutdown()

    def test_fetch_plain_reads_text(self):
        self.assertIn("hi", _backends.fetch_plain(self.base, {}, 1000, 5, {}))
        self.assertIn("hi", _fetch.fetch(self.base))


class TestRegistry(unittest.TestCase):
    def test_describe_covers_every_source(self):
        d = describe()
        ids = [s["id"] for s in d]
        self.assertEqual(ids, ["pdf", "docx", "epub", "ao3", "quantumbang", "web"])
        for s in d:
            self.assertIn("settings", s)
            self.assertIn("credential_hosts", s)

    def test_request_key_is_stable_per_input(self):
        a = request_key({"kind": "upload", "data": b"x"})
        self.assertEqual(a, request_key({"kind": "upload", "data": b"x"}))
        self.assertNotEqual(a, request_key({"kind": "upload", "data": b"y"}))
        self.assertEqual(request_key({"kind": "url", "url": "https://e.com"}),
                         request_key({"kind": "url", "url": "https://e.com"}))

    def test_ingest_runs_the_matching_source(self):
        s, key, book = ingest({"kind": "url", "url": "https://archiveofourown.org/works/9"},
                              fetcher=lambda url, cred=None, credential_hosts=(),
                              transport=None: AO3_PAGE)
        self.assertEqual(s.id, "ao3")  # loaded by the plugin host under its own module name
        self.assertEqual(book["meta"]["source"], "ao3")
        self.assertTrue(key)


if __name__ == "__main__":
    unittest.main()
