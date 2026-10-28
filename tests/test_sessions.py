"""Devices and sessions: see where an account is signed in, end any of them.

Exercised through the real cookie jar and the real routes, so a revoke is
proven by the other browser actually getting a 401 - not by a row count alone.
"""

import http.cookiejar
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from host import auth
from host.app import App, make_handler
from tests.test_app import READER_BODY, SPEECH_BODY, STORAGE_BODY
from tests.test_pluginhost import write_plugin
from tests import twofa_support


def _request(path, obj=None, port=0, ua=None):
    data = json.dumps(obj).encode() if obj is not None else None
    headers = {"Content-Type": "application/json"} if obj is not None else {}
    if ua:
        headers["User-Agent"] = ua
    return urllib.request.Request("http://127.0.0.1:%d%s" % (port, path),
                                  data=data, headers=headers)


def _browser():
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


class TestSessions(unittest.TestCase):
    UA_CHROME = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0 Safari/537.36"
    UA_PHONE = "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/120 Mobile Safari/537.36"

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

    def _req(self, op, path, ua=None):
        return op.open(_request(path, port=self.port, ua=ua))

    def _post(self, op, path, obj, ua=None):
        return op.open(_request(path, obj, port=self.port, ua=ua))

    def _jget(self, op, path, ua=None):
        with op.open(_request(path, port=self.port, ua=ua)) as r:
            return r.status, json.loads(r.read())

    def _jpost(self, op, path, obj, ua=None):
        with op.open(_request(path, obj, port=self.port, ua=ua)) as r:
            return r.status, json.loads(r.read())

    def _register(self, op, name="chris", ua=None):
        # the first account is an administrator, so it always owes a factor.
        # Clearing it here keeps every other test in this file about sessions.
        with self._post(op, "/auth/local/register",
                        {"username": name, "password": "longenough"}, ua=ua) as r:
            out = json.loads(r.read())
        if out.get("admin"):
            twofa_support.enrol(
                lambda path, obj: self._jpost(op, path, obj, ua=ua)[1], name)

    def _login(self, op, name="chris", ua=None):
        self._post(op, "/auth/local/login",
                   {"username": name, "password": "longenough"}, ua=ua)
        if name in twofa_support.SECRETS:
            twofa_support.finish(
                lambda path, obj: self._jpost(op, path, obj, ua=ua)[1], name)

    def test_sessions_lists_devices_and_flags_the_current_one(self):
        a = _browser()
        self._register(a, ua=self.UA_CHROME)
        s, d = self._jget(a, "/sessions", ua=self.UA_CHROME)
        self.assertEqual(s, 200)
        self.assertEqual(len(d["sessions"]), 1)
        row = d["sessions"][0]
        self.assertEqual(row["label"], "Chrome on Windows")
        self.assertTrue(row["current"])
        self.assertTrue(row["id"])
        self.assertNotIn("token", row)

    def test_a_second_login_shows_two_and_one_is_not_current(self):
        a = _browser()
        self._register(a, ua=self.UA_CHROME)
        b = _browser()
        self._login(b, ua=self.UA_PHONE)
        s, d = self._jget(a, "/sessions", ua=self.UA_CHROME)
        rows = d["sessions"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(sum(1 for r in rows if r["current"]), 1)
        labels = sorted(r["label"] for r in rows)
        self.assertEqual(labels, ["Chrome on Android", "Chrome on Windows"])

    def test_revoking_a_device_ends_that_session_and_only_that_one(self):
        a = _browser()
        self._register(a, ua=self.UA_CHROME)
        b = _browser()
        self._login(b, ua=self.UA_PHONE)
        _, d = self._jget(a, "/sessions", ua=self.UA_CHROME)
        other = next(r for r in d["sessions"] if not r["current"])
        self._post(a, "/sessions/revoke", {"id": other["id"]}, ua=self.UA_CHROME)
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._req(b, "/sessions")
        self.assertEqual(cm.exception.code, 401)
        # the caller's own session still works
        s, d2 = self._jget(a, "/sessions", ua=self.UA_CHROME)
        self.assertEqual(len(d2["sessions"]), 1)
        self.assertTrue(d2["sessions"][0]["current"])

    def test_log_out_everywhere_others_keeps_this_device(self):
        a = _browser()
        self._register(a, ua=self.UA_CHROME)
        b = _browser()
        self._login(b, ua=self.UA_PHONE)
        self._post(b, "/sessions/revoke_all", {"others": True}, ua=self.UA_PHONE)
        s, d2 = self._jget(b, "/sessions", ua=self.UA_PHONE)
        self.assertEqual(len(d2["sessions"]), 1)
        self.assertTrue(d2["sessions"][0]["current"])
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._req(a, "/sessions", ua=self.UA_CHROME)
        self.assertEqual(cm.exception.code, 401)

    def test_log_out_everywhere_including_this_device(self):
        a = _browser()
        self._register(a, ua=self.UA_CHROME)
        b = _browser()
        self._login(b, ua=self.UA_PHONE)
        self._post(a, "/sessions/revoke_all", {"others": False}, ua=self.UA_CHROME)
        for op, ua in ((a, self.UA_CHROME), (b, self.UA_PHONE)):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                self._req(op, "/sessions", ua=ua)
            self.assertEqual(cm.exception.code, 401)

    def test_a_revoke_is_scoped_to_the_caller(self):
        a = _browser()
        self._register(a, "chris", ua=self.UA_CHROME)
        c = _browser()
        self._register(c, "sam", ua=self.UA_PHONE)
        _, da = self._jget(a, "/sessions", ua=self.UA_CHROME)
        target = da["sessions"][0]["id"]
        s, _ = self._jget(c, "/sessions", ua=self.UA_PHONE)
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._post(c, "/sessions/revoke", {"id": target}, ua=self.UA_PHONE)
        self.assertEqual(cm.exception.code, 404)   # not sam's session to end
        s, da2 = self._jget(a, "/sessions", ua=self.UA_CHROME)
        self.assertEqual(len(da2["sessions"]), 1)  # chris is untouched

    def test_sessions_route_requires_a_session(self):
        op = _browser()
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._req(op, "/sessions")
        self.assertEqual(cm.exception.code, 401)

    def test_admin_sees_every_session_on_the_server(self):
        a = _browser()
        self._register(a, "chris", ua=self.UA_CHROME)      # first account = admin
        b = _browser()
        self._register(b, "sam", ua=self.UA_PHONE)
        s, d = self._jget(a, "/sessions", ua=self.UA_CHROME)
        self.assertEqual(s, 200)
        self.assertTrue(d.get("admin"))
        users = sorted(r["user"] for r in d["all"])
        self.assertIn("local:sam", users)
        self.assertIn("local:chris", users)
        self.assertEqual(len(d["sessions"]), 1)            # sam's is not in the own list

    def test_a_non_admin_gets_no_server_wide_list(self):
        a = _browser()
        self._register(a, "chris", ua=self.UA_CHROME)
        b = _browser()
        self._register(b, "sam", ua=self.UA_PHONE)
        s, d = self._jget(b, "/sessions", ua=self.UA_PHONE)
        self.assertEqual(s, 200)
        self.assertFalse(d.get("admin"))
        self.assertNotIn("all", d)

    def test_admin_boots_another_users_session(self):
        a = _browser()
        self._register(a, "chris", ua=self.UA_CHROME)
        b = _browser()
        self._register(b, "sam", ua=self.UA_PHONE)
        _, db = self._jget(b, "/sessions", ua=self.UA_PHONE)
        target = db["sessions"][0]["id"]
        s, da = self._jget(a, "/sessions", ua=self.UA_CHROME)
        s, d = self._jpost(a, "/sessions/revoke", {"id": target}, ua=self.UA_CHROME)
        self.assertEqual(s, 200)
        self.assertTrue(d["ok"])
        with self.assertRaises(urllib.error.HTTPError) as cm:   # sam is out
            self._jget(b, "/sessions", ua=self.UA_PHONE)
        self.assertEqual(cm.exception.code, 401)

    def test_admin_boot_everyone_keeps_the_admin_signed_in(self):
        a = _browser()
        self._register(a, "chris", ua=self.UA_CHROME)
        b = _browser()
        self._register(b, "sam", ua=self.UA_PHONE)
        s, d = self._jpost(a, "/sessions/revoke_all", {"all_users": True}, ua=self.UA_CHROME)
        self.assertEqual(s, 200)
        self.assertGreaterEqual(d["revoked"], 1)
        s, da = self._jget(a, "/sessions", ua=self.UA_CHROME)   # admin still on
        self.assertEqual(s, 200)
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._jget(b, "/sessions", ua=self.UA_PHONE)
        self.assertEqual(cm.exception.code, 401)

    def test_non_admin_cannot_boot_everyone(self):
        a = _browser()
        self._register(a, "chris", ua=self.UA_CHROME)
        b = _browser()
        self._register(b, "sam", ua=self.UA_PHONE)
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._jpost(b, "/sessions/revoke_all", {"all_users": True}, ua=self.UA_PHONE)
        self.assertEqual(cm.exception.code, 403)


class TestDeviceLabel(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(auth.device_label(""), "Unknown device")
        self.assertEqual(auth.device_label("Mozilla/5.0 (Windows NT 10.0) Chrome/120"),
                         "Chrome on Windows")
        self.assertEqual(auth.device_label("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0) "
                                           "Version/17.0 Mobile Safari/604.1"),
                         "Safari on iOS")
        self.assertEqual(auth.device_label("Mozilla/5.0 (X11; Linux x86_64) Firefox/121.0"),
                         "Firefox on Linux")
        self.assertEqual(auth.device_label("Mozilla/5.0 (Windows NT 10.0) Edg/120.0"),
                         "Edge on Windows")
        self.assertEqual(auth.device_label("curl/8.5.0"), "Browser")


class TestRevokeHelpers(unittest.TestCase):
    def setUp(self):
        import sqlite3
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        from host import db
        self.conn.executescript(db.SCHEMA)
        db.migrate(self.conn)

    def test_revoke_one_and_all(self):
        t1 = auth.create_session(self.conn, "u1", "local", ua="a")
        t2 = auth.create_session(self.conn, "u1", "local", ua="b")
        rows = auth.list_sessions(self.conn, "u1", t1)
        self.assertEqual(len(rows), 2)
        self.assertTrue([r for r in rows if r["current"] and r["id"]])
        sid = [r for r in rows if not r["current"]][0]["id"]
        self.assertTrue(auth.revoke_session(self.conn, "u1", sid))
        self.assertIsNone(auth.session_user(self.conn, t2))
        self.assertEqual(auth.session_user(self.conn, t1), "u1")
        n = auth.revoke_sessions(self.conn, "u1", keep_token="")
        self.assertEqual(n, 1)
        self.assertIsNone(auth.session_user(self.conn, t1))

    def test_keep_token_leaves_one_alive(self):
        t1 = auth.create_session(self.conn, "u1", "local")
        t2 = auth.create_session(self.conn, "u1", "local")
        n = auth.revoke_sessions(self.conn, "u1", keep_token=t1)
        self.assertEqual(n, 1)
        self.assertEqual(auth.session_user(self.conn, t1), "u1")
        self.assertIsNone(auth.session_user(self.conn, t2))

    def test_revoke_any_finds_the_owner_and_clears_it(self):
        t1 = auth.create_session(self.conn, "u1", "local", ua="chrome")
        t2 = auth.create_session(self.conn, "u2", "local", ua="phone")
        rows = auth.list_all_sessions(self.conn, t1)
        self.assertEqual(len(rows), 2)
        self.assertTrue([r for r in rows if r["current"] and r["user"] == "u1"])
        sid = [r for r in rows if r["user"] == "u2"][0]["id"]
        self.assertEqual(auth.revoke_any(self.conn, sid), "u2")
        self.assertIsNone(auth.session_user(self.conn, t2))
        self.assertEqual(auth.session_user(self.conn, t1), "u1")
        self.assertIsNone(auth.revoke_any(self.conn, "nosuchid"))

    def test_revoke_all_sessions_keeps_given_token(self):
        t1 = auth.create_session(self.conn, "u1", "local")
        t2 = auth.create_session(self.conn, "u2", "local")
        n = auth.revoke_all_sessions(self.conn, keep_token=t1)
        self.assertEqual(n, 1)
        self.assertEqual(auth.session_user(self.conn, t1), "u1")
        self.assertIsNone(auth.session_user(self.conn, t2))


if __name__ == "__main__":
    unittest.main()
