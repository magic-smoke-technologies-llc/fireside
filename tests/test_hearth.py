"""The door's fire: an optional file, served in slices, never a broken player.

The clip is licensed media the owner supplies, so the tests cover both states
the build can be in. The important half is the one that ships today, with the
file absent: a missing clip must read as "the door keeps its own glow", not as
a 500 or a video element pointed at a dead URL.

The range tests exist because the reveal depends on the browser being able to
seek. Chrome loops a small clip badly without 206 and Safari refuses it
outright, and neither failure shows up as an exception anywhere.
"""

import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import host.routes_static as hostapp   # the module that reads ASSETS_DIR
from host.app import App, make_handler
from tests.test_pluginhost import write_plugin

READER_BODY = """
import types
class Plugin:
    def matches(self, req):
        return req.get("kind") == "upload"
    def read(self, req, fetch, cred):
        return types.SimpleNamespace(title="T", meta={}, id=None, chapters=[])
    def settings(self):
        return []
    def credential_hosts(self):
        return []
"""

STORAGE_BODY = """
class Plugin:
    def __init__(self):
        self._d = {}
    def put(self, data):
        import hashlib
        r = hashlib.sha256(data).hexdigest(); self._d[r] = data; return r
    def has(self, ref):
        return ref in self._d
    def get(self, ref):
        return self._d[ref]
    def delete(self, ref):
        self._d.pop(ref, None)
    def info(self):
        return {"kind": "memory", "durable": False, "url_ttl": None}
"""


class HearthCase(unittest.TestCase):
    """A real host over a temp assets dir, so the file's presence is the only
    variable between the two states."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.assets = os.path.join(self.tmp, "assets")
        os.makedirs(self.assets)
        self._saved_assets = hostapp.ASSETS_DIR
        hostapp.ASSETS_DIR = self.assets
        root = os.path.join(self.tmp, "plugins")
        write_plugin(root, "reader", "fake", order=10, body=READER_BODY)
        write_plugin(root, "storage", "mem", body=STORAGE_BODY)
        self.app = App(root, db_path=os.path.join(self.tmp, "db.sqlite"),
                       storage_id="mem", require_auth=True)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        hostapp.ASSETS_DIR = self._saved_assets

    def put_clip(self, data, poster=None):
        with open(os.path.join(self.assets, "hearth.mp4"), "wb") as f:
            f.write(data)
        if poster is not None:
            with open(os.path.join(self.assets, "hearth-poster.jpg"), "wb") as f:
                f.write(poster)

    def get(self, path, headers=None):
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path),
                                     headers=headers or {})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def page(self):
        s, h, body = self.get("/")
        self.assertEqual(s, 200)
        return body.decode("utf-8")


class TestNoClipYet(HearthCase):

    def test_a_missing_clip_is_a_404_not_a_crash(self):
        s, _, body = self.get("/hearth.mp4")
        self.assertEqual(s, 404)
        s, _, _ = self.get("/hearth-poster.jpg")
        self.assertEqual(s, 404)

    def test_the_page_says_the_clip_is_absent(self):
        # The render, not a probe request, is what tells the page: otherwise
        # every door load in production pays for a 404.
        html = self.page()
        self.assertIn('data-has=""', html)
        self.assertNotIn("__HEARTH__", html)
        self.assertNotIn("__HEARTH_POSTER__", html)
        self.assertNotIn("poster=", html.split('id="hearth"')[1].split("</video>")[0])


class TestClipInTheBuild(HearthCase):

    CLIP = b"\x00\x00\x00\x18ftypmp42" + bytes(range(256)) * 40

    def test_the_clip_serves_as_video_and_is_public(self):
        # require_auth is on in setUp, yet the door's fire loads with no session.
        self.put_clip(self.CLIP)
        s, h, body = self.get("/hearth.mp4")
        self.assertEqual(s, 200)
        self.assertEqual(h.get("Content-Type"), "video/mp4")
        self.assertEqual(h.get("Accept-Ranges"), "bytes")
        self.assertEqual(body, self.CLIP)

    def test_a_range_returns_exactly_the_slice_asked_for(self):
        self.put_clip(self.CLIP)
        s, h, body = self.get("/hearth.mp4", {"Range": "bytes=10-19"})
        self.assertEqual(s, 206)
        self.assertEqual(body, self.CLIP[10:20])
        self.assertEqual(h.get("Content-Range"),
                         "bytes 10-19/%d" % len(self.CLIP))
        self.assertEqual(h.get("Content-Length"), "10")

    def test_an_open_ended_range_runs_to_the_end(self):
        self.put_clip(self.CLIP)
        s, h, body = self.get("/hearth.mp4", {"Range": "bytes=%d-" % (len(self.CLIP) - 5)})
        self.assertEqual(s, 206)
        self.assertEqual(body, self.CLIP[-5:])

    def test_a_suffix_range_is_the_tail(self):
        self.put_clip(self.CLIP)
        s, h, body = self.get("/hearth.mp4", {"Range": "bytes=-8"})
        self.assertEqual(s, 206)
        self.assertEqual(body, self.CLIP[-8:])

    def test_a_nonsense_range_gets_the_whole_file_rather_than_a_lie(self):
        # Two ranges cannot be answered with one slice; a body that is short of
        # what the browser asked for is read as a corrupt clip.
        self.put_clip(self.CLIP)
        s, _, body = self.get("/hearth.mp4", {"Range": "bytes=0-4,10-14"})
        self.assertEqual(s, 200)
        self.assertEqual(body, self.CLIP)
        s, _, body = self.get("/hearth.mp4", {"Range": "bytes=999999-"})
        self.assertEqual(s, 200)
        self.assertEqual(body, self.CLIP)

    def test_the_page_flags_the_clip_and_its_poster(self):
        self.put_clip(self.CLIP, poster=b"\xff\xd8\xff\xe0junk")
        html = self.page()
        self.assertIn('data-has="1"', html)
        self.assertIn('poster="/hearth-poster.jpg"', html)
        self.assertNotIn("__HEARTH", html)
        s, h, body = self.get("/hearth-poster.jpg")
        self.assertEqual(s, 200)
        self.assertEqual(h.get("Content-Type"), "image/jpeg")
        self.assertEqual(body, b"\xff\xd8\xff\xe0junk")

    def test_no_poster_means_no_poster_attribute(self):
        # A missing poster would be one more request that ends in 404.
        self.put_clip(self.CLIP)
        html = self.page()
        self.assertIn('data-has="1"', html)
        self.assertNotIn('poster="', html)


if __name__ == "__main__":
    unittest.main()