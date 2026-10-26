"""End-to-end over loopback: import -> render -> chapter -> playback handoff.

Uses fake plugins in a temp root, so it proves the wiring with no network.
"""

import hashlib
import json
import os
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

from host.app import App, make_handler
from tests.test_pluginhost import write_plugin

READER_BODY = """
import types
class Plugin:
    def matches(self, req):
        return req.get(\"kind\") == \"upload\"
    def read(self, req, fetch, cred):
        return types.SimpleNamespace(title=\"T\", meta={}, id=None,
            chapters=[types.SimpleNamespace(title=\"c0\", text=\"hello world\"),
                      types.SimpleNamespace(title=\"c1\", text=\"second chapter\")])
    def settings(self):
        return []
    def credential_hosts(self):
        return []
"""

SPEECH_BODY = """
from host.contracts import Audio, Quote, Report
class Plugin:
    id = \"fake\"
    ext = \"mp3\"
    mime = \"audio/mpeg\"
    needs_key = False
    def list_voices(self, cred):
        return [{\"id\": \"vv\", \"label\": \"Voice V\"}]
    def synth(self, text, voice, opts, cred):
        return Audio(data=(\"A:\" + voice + \":\" + text).encode())
    def cost_per_1m_chars(self, cred):
        return 12.0
    def quote(self, ctx):
        return Quote(chars_est=ctx.get(\"chars_est\", 0), price=0.0)
    def report(self, ctx):
        return Report(charge=\"charged\", cost=0.0, currency=\"USD\")
"""

STORAGE_BODY = """
import hashlib
class Plugin:
    def __init__(self):
        self._d = {}
    def put(self, data):
        r = hashlib.sha256(data).hexdigest()
        self._d[r] = data
        return r
    def has(self, ref):
        return ref in self._d
    def get(self, ref):
        return self._d[ref]
    def delete(self, ref):
        self._d.pop(ref, None)
    def info(self):
        return {\"kind\": \"memory\", \"durable\": False, \"url_ttl\": None}
"""


class TestApp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        root = os.path.join(self.tmp, "plugins")
        write_plugin(root, "reader", "fake", order=10, body=READER_BODY)
        write_plugin(root, "speech", "fake", body=SPEECH_BODY)
        write_plugin(root, "storage", "mem", body=STORAGE_BODY)
        self.app = App(root, db_path=os.path.join(self.tmp, "db.sqlite"),
                       storage_id="mem")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()

    def _get(self, path):
        with urllib.request.urlopen("http://127.0.0.1:%d%s" % (self.port, path)) as r:
            return r.status, r.read()

    def _post(self, path, obj):
        req = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.port, path),
            data=json.dumps(obj).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())

    def _jget(self, path):
        s, b = self._get(path)
        return s, json.loads(b)

    def _wait_job(self, jid, timeout=10):
        import time
        end = time.time() + timeout
        while time.time() < end:
            s, job = self._jget("/job?id=%s" % jid)
            if job["status"] in ("done", "error"):
                return job
            time.sleep(0.05)
        self.fail("job %s did not finish in %ss" % (jid, timeout))

    def test_health(self):
        s, body = self._jget("/health")
        self.assertEqual(s, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["storage"], ["mem"])

    def test_estimate_route(self):
        s, imp = self._post("/import", {"user": "u1", "kind": "upload",
                                        "filename": "x.epub"})
        bid = imp["book_id"]
        s, e = self._jget("/estimate?book=%s&provider=fake" % bid)
        self.assertEqual(s, 200)
        self.assertGreater(e["chars"], 0)
        self.assertGreater(e["synth_seconds"], 0)
        self.assertGreater(e["audio_bytes"], 0)
        s, one = self._jget("/estimate?book=%s&provider=fake&chapters=0" % bid)
        self.assertLess(one["chars"], e["chars"])

    def test_full_flow(self):
        s, imp = self._post("/import", {"user": "u1", "kind": "upload",
                                        "filename": "x.epub"})
        self.assertEqual(s, 200)
        bid = imp["book_id"]
        self.assertEqual(imp["chapters"], 2)

        s, lib = self._jget("/library?user=u1")
        self.assertEqual(len(lib), 1)

        s, r = self._post("/render", {"user": "u1", "book": bid,
                                      "provider": "fake", "voice": "v"})
        self.assertEqual(s, 200)
        job = self._wait_job(r["job_id"])
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["current"], 2)

        s, prog = self._jget("/progress?user=u1&book=%s" % bid)
        self.assertTrue(all(c["rendered"] for c in prog["chapters"]))

        s, audio = self._get("/chapter?book=%s&idx=0" % bid)
        self.assertEqual(audio, b"A:v:hello world")

    def test_multi_voice_renditions(self):
        # the same chapter voiced twice: both are kept, and the player can ask
        # for either by name. No voice named = the newest.
        s, imp = self._post("/import", {"user": "u1", "kind": "upload"})
        bid = imp["book_id"]
        for v in ("male", "female"):
            _, r = self._post("/render", {"user": "u1", "book": bid, "provider": "fake",
                                           "voice": v, "voice_label": v.title()})
            self._wait_job(r["job_id"])
        s, prog = self._jget("/progress?user=u1&book=%s" % bid)
        voices = {r["voice"] for c in prog["chapters"] for r in c["renditions"]}
        self.assertEqual(voices, {"male", "female"})
        labels = {r["voice_label"] for c in prog["chapters"] for r in c["renditions"]}
        self.assertEqual(labels, {"Male", "Female"})
        s, a = self._get("/chapter?book=%s&idx=0&provider=fake&voice=male" % bid)
        self.assertEqual(a, b"A:male:hello world")
        s, b = self._get("/chapter?book=%s&idx=0&provider=fake&voice=female" % bid)
        self.assertEqual(b, b"A:female:hello world")
        s, d = self._get("/chapter?book=%s&idx=0" % bid)
        self.assertEqual(d, b"A:female:hello world")
        import urllib.error
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._get("/chapter?book=%s&idx=0&voice=nobody" % bid)
        self.assertEqual(cm.exception.code, 409)

    def test_playback_handoff_over_http(self):
        s, imp = self._post("/import", {"user": "u1", "kind": "upload"})
        bid = imp["book_id"]
        self._post("/claim", {"user": "u1", "book": bid, "device": "pc"})
        self._post("/position", {"user": "u1", "book": bid, "chapter_index": 1,
                                 "offset_seconds": 3.5, "device": "pc"})
        self._post("/claim", {"user": "u1", "book": bid, "device": "phone"})
        s, stop = self._jget("/stop?user=u1&book=%s&device=pc" % bid)
        self.assertTrue(stop["stop"])
        s, pos = self._jget("/position?user=u1&book=%s" % bid)
        self.assertEqual(pos["chapter_index"], 1)

    def test_not_rendered_is_409(self):
        s, imp = self._post("/import", {"user": "u1", "kind": "upload"})
        import urllib.error
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._get("/chapter?book=%s&idx=0" % imp["book_id"])
        self.assertEqual(cm.exception.code, 409)

    def test_example_book_add_and_idempotent(self):
        s, d = self._post("/book/example", {"user": "u1"})
        self.assertEqual(s, 200)
        self.assertTrue(d["title"])
        self.assertGreaterEqual(d["chapters"], 1)
        self.assertFalse(d["existing"])
        s, lib = self._jget("/library?user=u1")
        self.assertEqual(len(lib), 1)
        # adding it again finds the copy already there, never a second book
        s, d2 = self._post("/book/example", {"user": "u1"})
        self.assertEqual(d2["book_id"], d["book_id"])
        self.assertTrue(d2["existing"])
        s, lib = self._jget("/library?user=u1")
        self.assertEqual(len(lib), 1)

    def test_example_book_free_render(self):
        # With a sale connection named, the example is voiced through it in one
        # call, and because the text is the same bytes for everyone the render
        # is a cache hit after the first. Here we prove the wiring: the route
        # adds the book and starts a render on the example voice.
        from host import connections
        cid = connections.upsert(self.app.conn, "u1", "fake", "Shared voice",
                                 {"k": "v"}, scope=connections.SYSTEM)
        self.app.sale_connection = {"connection_id": cid, "provider": "fake",
                                    "voice": "vv"}
        s, d = self._post("/book/example", {"user": "u1", "render": True})
        self.assertEqual(s, 200)
        self.assertEqual(d["render_status"], 200)
        self.assertIsNotNone(d["example_voice"])
        job = self._wait_job(d["render"]["job_id"])
        self.assertEqual(job["status"], "done")
        s, prog = self._jget("/progress?user=u1&book=%s" % d["book_id"])
        self.assertTrue(all(c["rendered"] for c in prog["chapters"]))

    def test_example_book_carries_its_record(self):
        # A free book with a title and nothing else reads as an empty record:
        # no author line, no blurb, no cover. The bundled asset is the one
        # source for all of it, and the date and publisher are real (David
        # Nutt, London, 1888) rather than invented to fill the panel.
        s, d = self._post("/book/example", {"user": "u1"})
        self.assertEqual(s, 200)
        s, prog = self._jget("/progress?user=u1&book=%s" % d["book_id"])
        b = prog["book"]
        self.assertEqual(b["author"], "Oscar Wilde")
        self.assertEqual(b["publisher"], "David Nutt, London")
        self.assertEqual(b["published"], "1888")
        self.assertIn("fairy tales", b["description"])
        self.assertEqual(b["isbn"], "")   # a PG text has no ISBN; don't fake one
        s, cover = self._get("/cover?book=%s" % d["book_id"])
        self.assertEqual(s, 200)
        self.assertTrue(cover.startswith(b"\xff\xd8"))   # a real jpeg

    def test_example_book_fills_an_old_copy_blanks_only(self):
        # Copies added before the record was complete must gain it, and the
        # reader's own words must survive.
        from host import library
        s, d = self._post("/book/example", {"user": "u1"})
        bid = d["book_id"]
        self.app.conn.execute(
            "UPDATE books SET author='',publisher='',published='',description=?"
            " WHERE id=?", ("my own blurb", bid))
        self.app.conn.commit()
        s, d2 = self._post("/book/example", {"user": "u1"})
        self.assertTrue(d2["existing"])
        b = library.get_book(self.app.conn, bid)
        self.assertEqual(b["author"], "Oscar Wilde")
        self.assertEqual(b["publisher"], "David Nutt, London")
        self.assertEqual(b["description"], "my own blurb")

    def test_text_and_progress_fields(self):
        # the story page reads /text; the player counts chars from /progress
        s, imp = self._post("/import", {"user": "u1", "kind": "upload"})
        bid = imp["book_id"]
        s, tx = self._jget("/text?book=%s&idx=0" % bid)
        self.assertEqual(s, 200)
        self.assertEqual(tx["text"], "hello world")
        self.assertEqual(tx["title"], "c0")
        # the same text as reading blocks, so the player can mark the sentence
        # it is speaking. one block here: "hello world" has no sentence end.
        self.assertEqual([b["t"] for b in tx["sentences"]], ["hello world"])
        self.assertEqual(tx["sentences"][0]["p"], 0)
        import urllib.error
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._get("/text?book=%s&idx=99" % bid)
        self.assertEqual(cm.exception.code, 404)
        s, prog = self._jget("/progress?user=u1&book=%s" % bid)
        self.assertEqual(prog["chapters"][1]["title"], "c1")
        self.assertEqual(prog["chapters"][0]["chars"], len("hello world"))

    def test_voices_endpoint(self):
        s, d = self._post("/voices", {"provider": "fake"})
        self.assertEqual(s, 200)
        self.assertEqual(d["voices"][0]["id"], "vv")
        import urllib.error
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._post("/voices", {"provider": "nope"})
        self.assertEqual(cm.exception.code, 400)

    def test_speak_sandbox(self):
        # the voice sandbox says free text in one voice and hands the audio
        # straight back, with no book and nothing stored.
        req = urllib.request.Request(
            "http://127.0.0.1:%d/speak" % self.port,
            data=json.dumps({"provider": "fake", "voice": "vv",
                             "text": "hi there"}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as r:
            self.assertEqual(r.status, 200)
            self.assertEqual(r.read(), b"A:vv:hi there")

    def test_speak_rejects_over_1000_chars(self):
        import urllib.error
        req = urllib.request.Request(
            "http://127.0.0.1:%d/speak" % self.port,
            data=json.dumps({"provider": "fake", "voice": "vv",
                             "text": "x" * 1001}).encode(),
            headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 400)

    def test_speak_needs_text(self):
        import urllib.error
        req = urllib.request.Request(
            "http://127.0.0.1:%d/speak" % self.port,
            data=json.dumps({"provider": "fake", "voice": "vv",
                             "text": "  "}).encode(),
            headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 400)

    def test_jobs_list_and_cancel_finished(self):
        s, imp = self._post("/import", {"user": "u1", "kind": "upload"})
        bid = imp["book_id"]
        _, r = self._post("/render", {"user": "u1", "book": bid,
                                      "provider": "fake", "voice": "v"})
        s, jl = self._jget("/jobs?user=u1")
        self.assertEqual(s, 200)
        self.assertIn(r["job_id"], [j["id"] for j in jl["jobs"]])
        job = self._wait_job(r["job_id"])
        self.assertEqual(job["status"], "done")
        s, c = self._post("/cancel", {"user": "u1", "job": r["job_id"]})
        self.assertEqual(s, 200)
        self.assertFalse(c["cancel"])
        self.assertEqual(c["reason"], "done")
        import urllib.error
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._post("/cancel", {"user": "u2", "job": r["job_id"]})
        self.assertEqual(cm.exception.code, 403)

    def test_stats_and_listen_over_http(self):
        # chars come from finished render jobs; listen time is an append-only
        # log written by the play heartbeat.
        s, imp = self._post("/import", {"user": "u1", "kind": "upload"})
        bid = imp["book_id"]
        _, r = self._post("/render", {"user": "u1", "book": bid,
                                       "provider": "fake", "voice": "v"})
        self._wait_job(r["job_id"])
        s, st = self._jget("/stats?user=u1")
        self.assertEqual(s, 200)
        self.assertEqual(st["chars_total"], len("hello world") + len("second chapter"))
        self.assertEqual(st["chars_by_provider"].get("fake"),
                         len("hello world") + len("second chapter"))
        self.assertEqual(st["books_listened"], 0)
        self._post("/listen", {"user": "u1", "book": bid, "seconds": 12.5})
        self._post("/listen", {"user": "u1", "book": bid, "seconds": 7.5})
        s, st = self._jget("/stats?user=u1")
        self.assertEqual(st["books_listened"], 1)
        self.assertEqual(st["listen_seconds"], 20.0)


if __name__ == "__main__":
    unittest.main()

