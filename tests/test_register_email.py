"""A hosted server refuses to make an account it could never find again.

The address on a password account is the only key a social sign-in has to spot
that account (auth.account_for_email). An account without one can never be
matched, so the same person signing in with Google silently gets a second
account with its own library - #159. The unit rules live in test_local_auth;
this boots the real hosted server and proves the rule reaches the door.
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
from tests.test_app import READER_BODY, SPEECH_BODY, STORAGE_BODY
from tests.test_pluginhost import write_plugin


class HostedRegister(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        root = os.path.join(self.tmp, "plugins")
        write_plugin(root, "reader", "fake", order=10, body=READER_BODY)
        write_plugin(root, "speech", "fake", body=SPEECH_BODY)
        write_plugin(root, "storage", "mem", body=STORAGE_BODY)
        self.app = App(root, db_path=os.path.join(self.tmp, "db.sqlite"),
                       storage_id="mem", hosted=True, require_auth=True)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()

    def register(self, body):
        req = urllib.request.Request(
            "http://127.0.0.1:%d/auth/local/register" % self.port,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "{}")

    def test_hosted_refuses_a_registration_with_no_email(self):
        code, body = self.register({"username": "chris", "password": "hunter2hunter2"})
        self.assertEqual(code, 401)
        self.assertIn("email", body.get("error", ""))
        self.assertEqual(self.app.conn.execute(
            "SELECT COUNT(*) c FROM local_accounts").fetchone()["c"], 0)

    def test_hosted_accepts_a_registration_with_an_email(self):
        code, _ = self.register({"username": "chris", "password": "hunter2hunter2",
                                 "email": "chris@example.com"})
        self.assertEqual(code, 200)
        row = self.app.conn.execute(
            "SELECT email FROM local_accounts WHERE username=?", ("chris",)).fetchone()
        self.assertEqual(row["email"], "chris@example.com")


if __name__ == "__main__":
    unittest.main()
