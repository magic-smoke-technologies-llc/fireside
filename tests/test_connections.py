"""Voice-server connections on the server: shared config, per-user overrides.

The point of the model, in one sentence: a secret lives on the server, an admin
shares a connection with the household, a user may not see the secret but may
turn the connection on or off for themselves and change its open fields, and
none of that touches anyone else.

These tests drive the real cookie sessions and the real routes.
"""

import http.cookiejar
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from host.app import App, make_handler
from tests.test_app import READER_BODY, STORAGE_BODY
from tests.test_pluginhost import write_plugin
from tests import twofa_support

# A speech provider with one secret field and one open field, so redaction and
# override have something to act on. synth echoes the resolved secret and region
# so a test can prove what actually reached the provider.
SPEECH_BODY = '''
from host.contracts import Audio, Quote, Report
class Plugin:
    id = "fake"
    ext = "mp3"
    mime = "audio/mpeg"
    needs_key = True
    def settings(self):
        return [{"name": "key", "label": "Key", "kind": "password"},
                {"name": "region", "label": "Region", "kind": "text", "default": "eastus"}]
    def list_voices(self, cred):
        return [{"id": "vv", "label": "Voice V"}]
    def synth(self, text, voice, opts, cred):
        return Audio(data=("A:%s:%s:%s" % (cred.get("key", ""), cred.get("region", ""), text)).encode())
    def cost_per_1m_chars(self, cred):
        return 12.0
    def quote(self, ctx):
        return Quote(chars_est=ctx.get("chars_est", 0), price=0.0)
    def report(self, ctx):
        return Report(charge="charged", cost=0.0, currency="USD")
'''


def _req(path, obj, port):
    return urllib.request.Request(
        "http://127.0.0.1:%d%s" % (port, path),
        data=json.dumps(obj).encode(),
        headers={"Content-Type": "application/json"})


class ConnBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        root = os.path.join(self.tmp, "plugins")
        write_plugin(root, "reader", "fake", order=10, body=READER_BODY)
        write_plugin(root, "speech", "fake", body=SPEECH_BODY)
        write_plugin(root, "storage", "mem", body=STORAGE_BODY)
        self.app = App(root, db_path=os.path.join(self.tmp, "db.sqlite"),
                       storage_id="mem", require_auth=True)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()

    def browser(self):
        return urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def signup(self, b, u="chris", p="longenough"):
        # the first account is an administrator, and an administrator now always
        # owes a factor: enrol it so the rest of the suite is about connections.
        with b.open(_req("/auth/local/register", {"username": u, "password": p}, self.port)) as r:
            out = json.loads(r.read())
        if out.get("admin"):
            twofa_support.enrol(
                lambda path, obj: self.post(b, path, obj), u)
        return out

    def post(self, b, path, obj):
        with b.open(_req(path, obj, self.port)) as r:
            return json.loads(r.read())

    def get(self, b, path):
        with b.open("http://127.0.0.1:%d%s" % (self.port, path)) as r:
            return json.loads(r.read())

    def _wait(self, b, jid, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            j = self.get(b, "/job?id=" + jid)
            if j["status"] in ("done", "error"):
                return j
            time.sleep(0.05)
        self.fail("job %s did not finish" % jid)

    def _chapter(self, b, bid, idx=0):
        with b.open("http://127.0.0.1:%d/chapter?book=%s&idx=%d" % (self.port, bid, idx)) as r:
            return r.read()

    def _admin_with_house(self, data=None, **kw):
        admin = self.browser()
        self.signup(admin)
        body = {"provider": "fake", "label": "House", "scope": "system",
                "data": data if data is not None else {"key": "SECRET", "region": "westus"}}
        body.update(kw)
        cid = self.post(admin, "/connections", body)["id"]
        return admin, cid


class TestConnections(ConnBase):
    def test_a_secret_never_leaves_the_server(self):
        admin, cid = self._admin_with_house()
        d = self.get(admin, "/connections")
        c = next(x for x in d["connections"] if x["id"] == cid)
        by = {f["name"]: f for f in c["fields"]}
        self.assertTrue(by["key"]["sensitive"])
        self.assertTrue(by["key"]["set"])
        self.assertEqual(by["key"]["value"], "")       # the value is not sent
        self.assertEqual(by["region"]["value"], "westus")  # open fields are
        self.assertNotIn("SECRET", json.dumps(d))

    def test_a_user_uses_the_shared_secret_without_seeing_it(self):
        admin, cid = self._admin_with_house()
        u = self.browser()
        self.signup(u, "cat")
        c = next(x for x in self.get(u, "/connections")["connections"] if x["id"] == cid)
        self.assertEqual(c["scope"], "system")
        self.assertFalse(c["editable"])
        by = {f["name"]: f for f in c["fields"]}
        self.assertTrue(by["key"]["set"])
        self.assertEqual(by["key"]["value"], "")
        # the render resolves the admin's secret in-process
        bid = self.post(u, "/import", {"kind": "upload", "filename": "x.epub"})["book_id"]
        r = self.post(u, "/render", {"book": bid, "connection_id": cid, "voice": "vv"})
        self.assertEqual(self._wait(u, r["job_id"])["status"], "done")
        self.assertIn(b"SECRET", self._chapter(u, bid))

    def test_a_user_override_is_honoured_and_the_secret_is_not_writable(self):
        admin, cid = self._admin_with_house()
        u = self.browser()
        self.signup(u, "cat")
        # she changes the open field and tries to overwrite the secret
        self.post(u, "/connections/override",
                  {"id": cid, "data": {"region": "eastus", "key": "NEW"}})
        bid = self.post(u, "/import", {"kind": "upload", "filename": "x.epub"})["book_id"]
        r = self.post(u, "/render", {"book": bid, "connection_id": cid, "voice": "vv"})
        self._wait(u, r["job_id"])
        audio = self._chapter(u, bid)
        self.assertIn(b":SECRET:", audio)     # the secret is unchanged
        self.assertIn(b":eastus:", audio)     # her region override took

    def test_switching_a_shared_server_off_is_per_user(self):
        admin, cid = self._admin_with_house()
        u = self.browser()
        self.signup(u, "cat")
        self.post(u, "/connections/override", {"id": cid, "enabled": False})
        bid = self.post(admin, "/import", {"kind": "upload", "filename": "x.epub"})["book_id"]
        with self.assertRaises(urllib.error.HTTPError) as cm:
            u.open(_req("/render", {"book": bid, "connection_id": cid, "voice": "vv"}, self.port))
        self.assertEqual(cm.exception.code, 403)
        # the admin is untouched
        r = self.post(admin, "/render", {"book": bid, "connection_id": cid, "voice": "vv"})
        self.assertEqual(self._wait(admin, r["job_id"])["status"], "done")

    def test_a_personal_server_is_only_yours(self):
        admin = self.browser()
        self.signup(admin)
        self.post(admin, "/connections",
                  {"provider": "fake", "label": "Mine", "data": {"key": "K"}})
        u = self.browser()
        self.signup(u, "cat")
        self.assertEqual(self.get(u, "/connections")["connections"], [])

    def test_only_an_admin_shares_a_server(self):
        admin = self.browser()
        self.signup(admin)
        u = self.browser()
        self.signup(u, "cat")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            u.open(_req("/connections",
                        {"provider": "fake", "scope": "system", "data": {"key": "x"}}, self.port))
        self.assertEqual(cm.exception.code, 403)

    def test_default_voice_is_per_user(self):
        admin, cid = self._admin_with_house()
        u = self.browser()
        self.signup(u, "cat")
        self.post(u, "/prefs", {"default_connection_id": cid, "default_voice": "vv"})
        d = self.get(u, "/connections")
        self.assertEqual(d["default_connection_id"], cid)
        self.assertEqual(d["default_voice"], "vv")
        # the admin set nothing, so their default is untouched
        self.assertEqual(self.get(admin, "/connections")["default_voice"], "")
    def test_reading_prefs_are_per_user_and_survive_a_voice_save(self):
        admin, cid = self._admin_with_house()
        u = self.browser()
        self.signup(u, "cat")
        # defaults before anything is saved
        d = self.get(u, "/connections")["reading"]
        self.assertEqual(d, {"highlight": True, "scroll": True, "mode": "follow"})
        self.post(u, "/prefs", {"reading": {"highlight": True, "scroll": False, "mode": "center"}})
        d = self.get(u, "/connections")["reading"]
        self.assertEqual(d, {"highlight": True, "scroll": False, "mode": "center"})
        # saving a default voice is a different row write; it must not wipe it
        self.post(u, "/prefs", {"default_connection_id": cid, "default_voice": "vv"})
        d = self.get(u, "/connections")["reading"]
        self.assertEqual(d, {"highlight": True, "scroll": False, "mode": "center"})
        # a partial update merges into what is stored
        self.post(u, "/prefs", {"reading": {"scroll": True}})
        self.assertEqual(self.get(u, "/connections")["reading"]["mode"], "center")
        # a mode nobody offers falls back instead of sticking
        self.post(u, "/prefs", {"reading": {"mode": "sideways", "highlight": False}})
        d = self.get(u, "/connections")["reading"]
        self.assertEqual(d, {"highlight": False, "scroll": True, "mode": "center"})
        # and none of it reached the admin
        self.assertEqual(self.get(admin, "/connections")["reading"]["mode"], "follow")
