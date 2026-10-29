"""A series page is several books: list what is there, pull what was picked.

Two layers, both on unittest.TestCase because CI runs unittest discover.

The reader half is the site's real shape: a ~Series~ landing page whose Stories
tab lists each work as a title link followed by its word count, works in reading
order, and some of those works published in -N-M parts of their own. The host
half is the contract: the list comes from the page, never from the request, and
each work goes through the ordinary import path so duplicate, quota and replace
rules are the same as they are for a single book.
"""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from host.app import App, make_handler
from tests.test_app import SPEECH_BODY, STORAGE_BODY
from tests.test_pluginhost import write_plugin

SERIES_PAGE = "https://series.example/fan-fiction/one/"
W1 = "https://series.example/2021/06/first-1-2/"
W2 = "https://series.example/2021/09/second-2/"
W3 = "https://series.example/2022/10/third-1-4/"
OFF_PAGE = "https://series.example/2021/01/not-on-this-page/"

READER_SERIES = '''
import types
from host.contracts import Refused, Book, Chapter

PAGE = "%s"
WORKS = [
    {"url": "%s", "title": "First", "words": 12000, "parts": 2},
    {"url": "%s", "title": "Second", "words": 8000, "parts": None},
    {"url": "%s", "title": "Third", "words": 40000, "parts": 4},
]
TITLE = "One"
BROKEN = ""                     # set by a test: this work's read() raises


def _book(url):
    title = next((w["title"] for w in WORKS if w["url"] == url), "Work")
    return Book(title=title,
                chapters=[Chapter("Chapter 1", "text of " + title + " " * 20)],
                meta={"source": "series", "url": url, "identity": "series:" + title})


class Plugin:
    def matches(self, req):
        return req.get("kind") == "url" and str(req.get("url") or "").startswith("https://series.example/")

    def series(self, req, fetch, cred=None):
        if req.get("url") != PAGE:
            return None
        return {"title": TITLE, "page": PAGE, "works": [dict(w) for w in WORKS]}

    def read(self, req, fetch, cred):
        url = req.get("url")
        if url == PAGE:
            raise Refused("that is a series page", "pick the ones you want",
                          status=422,
                          series={"title": TITLE, "page": PAGE,
                                  "works": [dict(w) for w in WORKS]})
        if url == getattr(self, "BROKEN", ""):
            raise ValueError("no readable text on that page")
        if url not in [w["url"] for w in WORKS]:
            raise ValueError("not part of this series")
        return _book(url)

    def same(self, stored, incoming):
        a = (stored or {}).get("url")
        b = (incoming or {}).get("url")
        return bool(a and b and a == b)

    def settings(self): return []
    def credential_hosts(self): return []
''' % (SERIES_PAGE, W1, W2, W3)


class TestSeriesHttp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        root = os.path.join(self.tmp, "plugins")
        write_plugin(root, "reader", "series", order=10, body=READER_SERIES)
        write_plugin(root, "speech", "fake", body=SPEECH_BODY)
        write_plugin(root, "storage", "mem", body=STORAGE_BODY)
        self.app = App(root, db_path=os.path.join(self.tmp, "db.sqlite"),
                       storage_id="mem")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()

    def _post(self, path, obj):
        req = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.port, path),
            data=json.dumps(obj).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def _shelf(self):
        with urllib.request.urlopen(
                "http://127.0.0.1:%d/library?user=u1" % self.port) as r:
            return json.loads(r.read())

    def _pull(self, picks):
        return self._post("/import/series",
                          {"user": "u1", "url": SERIES_PAGE, "picks": picks})

    # ---- the refusal names the list ------------------------------------------

    def test_pasting_a_series_page_lists_it_instead_of_folding_it(self):
        s, out = self._post("/import", {"user": "u1", "kind": "url",
                                        "url": SERIES_PAGE})
        self.assertEqual(s, 422)
        self.assertEqual(out["error"], "that is a series page")
        ser = out["series"]
        self.assertEqual(ser["title"], "One")
        self.assertEqual([w["title"] for w in ser["works"]],
                         ["First", "Second", "Third"])
        self.assertEqual([w["words"] for w in ser["works"]],
                         [12000, 8000, 40000])
        self.assertEqual([w["parts"] for w in ser["works"]], [2, None, 4])
        self.assertEqual(self._shelf(), [])

    # ---- the pick imports the picks, and only the picks ----------------------

    def test_only_the_picked_works_land(self):
        s, out = self._pull([W1, W3])
        self.assertEqual(s, 200)
        self.assertEqual([r["title"] for r in out["imported"]], ["First", "Third"])
        self.assertEqual(out["failed"], [])
        self.assertEqual([b["title"] for b in self._shelf()], ["First", "Third"])

    def test_the_books_land_in_one_series_in_page_order(self):
        self._pull([W3, W1, W2])          # request order is not shelf order
        series = self.app.conn.execute(
            "SELECT name FROM series WHERE user_id='u1'").fetchall()
        self.assertEqual([r["name"] for r in series], ["One"])
        rows = self.app.conn.execute(
            "SELECT b.title FROM series_books sb JOIN books b ON b.id=sb.book_id "
            "ORDER BY sb.place").fetchall()
        self.assertEqual([r["title"] for r in rows], ["First", "Second", "Third"])

    def test_a_url_the_page_does_not_list_is_never_fetched(self):
        s, out = self._pull([OFF_PAGE])
        self.assertEqual(s, 400)
        self.assertEqual(out["unknown"], [OFF_PAGE])
        self.assertEqual(self._shelf(), [])

    def test_a_listed_url_mixed_with_an_unlisted_one_imports_only_the_listed(self):
        s, out = self._pull([OFF_PAGE, W2])
        self.assertEqual(s, 200)
        self.assertEqual([r["title"] for r in out["imported"]], ["Second"])
        self.assertEqual(out["unknown"], [OFF_PAGE])
        self.assertEqual([b["title"] for b in self._shelf()], ["Second"])

    def test_nothing_picked_is_a_bad_request_not_an_empty_success(self):
        s, out = self._pull([])
        self.assertEqual(s, 400)
        self.assertEqual(self._shelf(), [])

    def test_pulling_again_marks_what_is_already_here_instead_of_duplicating(self):
        self._pull([W1, W2])
        s, out = self._post("/import", {"user": "u1", "kind": "url",
                                        "url": SERIES_PAGE})
        self.assertEqual(s, 422)
        for w in out["series"]["works"][:2]:
            self.assertIn("existing", w)
        self.assertNotIn("existing", out["series"]["works"][2])

        s, out = self._pull([W1, W2, W3])
        self.assertEqual(s, 200)
        already = [r.get("already") for r in out["imported"]]
        self.assertEqual(already, [True, True, None])
        self.assertEqual(len(self._shelf()), 3)

    def test_one_work_failing_does_not_fail_the_batch(self):
        # A listed work whose read raises (a page the site changed, a login
        # wall): the other one still lands and the failure is reported per work.
        self.app.host.readers[0].obj.BROKEN = W3
        s, out = self._pull([W1, W3])
        self.assertEqual(s, 200)
        self.assertEqual([r["title"] for r in out["imported"]], ["First"])
        self.assertEqual([r["title"] for r in out["failed"]], ["Third"])
        self.assertIn("no readable text", out["failed"][0]["detail"])
        self.assertEqual([b["title"] for b in self._shelf()], ["First"])


if __name__ == "__main__":
    unittest.main()