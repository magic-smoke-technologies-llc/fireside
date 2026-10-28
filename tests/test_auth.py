"""Local accounts: the door, the first admin, and the registration switch.

Every request goes through a cookie jar, so these tests exercise the real
session cookie, not a stubbed user id.
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


def _open(path, obj, port):
    req = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (port, path),
        data=json.dumps(obj).encode(),
        headers={"Content-Type": "application/json"})
    return req


class TestAuth(unittest.TestCase):
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
        self.jar = http.cookiejar.CookieJar()
        self.browser = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))

    def tearDown(self):
        self.httpd.shutdown()

    def _post(self, path, obj):
        return self.browser.open(_open(path, obj, self.port))

    def _jget(self, path):
        with self.browser.open("http://127.0.0.1:%d%s" % (self.port, path)) as r:
            return r.status, json.loads(r.read())

    def _signup(self, name="chris"):
        """Register the first account and clear its factor.

        The first account is the administrator; an administrator always owes a
        second factor, so a test that needs a live session has to finish one.
        """
        with self._post("/auth/local/register",
                        {"username": name, "password": "longenough"}) as r:
            out = json.loads(r.read())
        if out.get("admin"):
            twofa_support.enrol(
                lambda path, obj: json.loads(self._post(path, obj).read()), name)
        return out

    def test_a_session_gates_the_app(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.browser.open("http://127.0.0.1:%d/library?user=x" % self.port)
        self.assertEqual(cm.exception.code, 401)

    def test_policy_is_public_and_reports_a_fresh_server(self):
        s, pol = self._jget("/auth/policy")
        self.assertEqual(s, 200)
        self.assertFalse(pol["has_accounts"])
        self.assertTrue(pol["registration_open"])

    def test_first_account_is_admin(self):
        self.assertTrue(self._signup()["admin"])
        s, me = self._jget("/me")
        self.assertTrue(me["authenticated"])
        self.assertTrue(me["admin"])
        s, pol = self._jget("/auth/policy")
        self.assertTrue(pol["has_accounts"])

    def test_wrong_password_says_no_but_not_which(self):
        self._post("/auth/local/register", {"username": "chris", "password": "longenough"})
        self.jar.clear()
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._post("/auth/local/login", {"username": "chris", "password": "wrongpass"})
        self.assertEqual(cm.exception.code, 401)
        self.assertIn("invalid username or password",
                      json.loads(cm.exception.read())["error"])

    def test_registration_can_close_and_the_first_account_is_still_let_in(self):
        # a stranger cannot be created once the admin closes the door
        self._signup()
        with self._post("/auth/registration", {"open": False}) as r:
            self.assertEqual(r.status, 200)
        stranger = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        with self.assertRaises(urllib.error.HTTPError) as cm:
            stranger.open(_open("/auth/local/register",
                                {"username": "sam", "password": "longenough"}, self.port))
        self.assertEqual(cm.exception.code, 401)
        self.assertIn("registration is closed", json.loads(cm.exception.read())["error"])
        # but a server with no accounts yet always lets the first one in
        fresh = auth.account_count(self.app.conn)
        self.assertEqual(fresh, 1)

    def test_only_an_admin_may_list_or_close_registration(self):
        # no session: refused
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._jget("/auth/users")
        self.assertEqual(cm.exception.code, 401)
        # a plain account is not an admin
        self._post("/auth/local/register", {"username": "chris", "password": "longenough"})
        self._post("/auth/local/register", {"username": "sam", "password": "longenough"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._jget("/auth/users")
        self.assertEqual(cm.exception.code, 403)

    # ---- linking a provider to an already-signed-in account ----------------

    def _wire_fake_provider(self):
        """A provider whose token + userinfo are answered locally, no network."""
        table = self.app.auth.table
        table["fake"] = {"label": "Fake", "authorize": "http://id.test/authorize",
                         "token": "http://id.test/token", "identity": "http://id.test/me",
                         "scope": "profile",
                         "id_env": ("FIRESIDE_OAUTH_FAKE_CLIENT_ID",
                                    "FIRESIDE_OAUTH_FAKE_SECRET")}
        os.environ["FIRESIDE_OAUTH_FAKE_CLIENT_ID"] = "cid"
        os.environ["FIRESIDE_OAUTH_FAKE_SECRET"] = "sec"

        def fetch(req, timeout=20):
            if req.full_url == "http://id.test/token":
                return json.dumps({"access_token": "tok"}).encode()
            return json.dumps({"sub": "u1", "name": "Chris",
                               "email": "chris@x.com", "email_verified": True}).encode()
        self.app.auth.fetch = fetch

    def _get_no_redirect(self, url):
        """Open without following the redirect, but still on the cookie jar."""
        class _Stop(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        op = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar), _Stop)
        try:
            return op.open(url)
        except urllib.error.HTTPError as e:
            return e

    def _state_cookie(self):
        for c in self.jar:
            if c.name == auth.STATE_COOKIE:
                return c.value
        return ""

    def test_start_without_a_session_refuses_to_link(self):
        self._wire_fake_provider()
        r = self._get_no_redirect("http://127.0.0.1:%d/auth/fake/start?link=1" % self.port)
        self.assertEqual(r.code, 401)

    def test_link_attaches_the_provider_to_the_signed_in_account(self):
        self._signup()
        self._wire_fake_provider()
        r = self._get_no_redirect("http://127.0.0.1:%d/auth/fake/start?link=1" % self.port)
        self.assertEqual(r.code, 302)
        self.assertIn("http://id.test/authorize", r.headers.get("Location", ""))
        # the callback ends the flow, and it never mints a new session
        before = next(c.value for c in self.jar if c.name == auth.SESSION_COOKIE)
        self._get_no_redirect("http://127.0.0.1:%d/auth/fake/callback?code=abc&state=%s"
                              % (self.port, self._state_cookie()))
        after = next(c.value for c in self.jar if c.name == auth.SESSION_COOKIE)
        self.assertEqual(before, after)
        s, d = self._jget("/auth/linked")
        self.assertEqual(d["linked"], ["fake"])

    # ---- signing in from the Android app ------------------------------------

    def test_app_start_returns_the_token_instead_of_a_cookie(self):
        self._wire_fake_provider()
        r = self._get_no_redirect(
            "http://127.0.0.1:%d/auth/fake/start?app=1" % self.port)
        self.assertEqual(r.code, 302)
        self.assertIn("http://id.test/authorize", r.headers.get("Location", ""))
        cookies = {c.name: c.value for c in self.jar}
        self.assertEqual(cookies.get(auth.APP_COOKIE), "1")

    def test_app_callback_hands_back_a_one_time_token_and_no_session(self):
        self._wire_fake_provider()
        self._get_no_redirect("http://127.0.0.1:%d/auth/fake/start?app=1" % self.port)
        before = {c.name for c in self.jar}
        r = self._get_no_redirect(
            "http://127.0.0.1:%d/auth/fake/callback?code=abc&state=%s"
            % (self.port, self._state_cookie()))
        self.assertEqual(r.code, 302)
        self.assertIn("/auth/app/done?token=", r.headers.get("Location", ""))
        # the tab is not signed in: no session cookie was set here
        self.assertNotIn(auth.SESSION_COOKIE, {c.name for c in self.jar})
        self.assertIn(auth.STATE_COOKIE, before)

    def test_the_done_page_offers_the_app_link(self):
        self._wire_fake_provider()
        self._get_no_redirect("http://127.0.0.1:%d/auth/fake/start?app=1" % self.port)
        r = self._get_no_redirect(
            "http://127.0.0.1:%d/auth/fake/callback?code=abc&state=%s"
            % (self.port, self._state_cookie()))
        loc = r.headers["Location"]
        page = self._get_no_redirect("http://127.0.0.1:%d%s" % (self.port, loc))
        html = page.read().decode()
        self.assertIn("fireside://auth?token=", html)
        self.assertIn("Open fireside", html)

    def test_the_webview_redeems_the_token_once(self):
        self._wire_fake_provider()
        self._get_no_redirect("http://127.0.0.1:%d/auth/fake/start?app=1" % self.port)
        r = self._get_no_redirect(
            "http://127.0.0.1:%d/auth/fake/callback?code=abc&state=%s"
            % (self.port, self._state_cookie()))
        hand = r.headers["Location"].split("token=")[1]
        # a fresh jar stands in for the app's webview
        self.jar.clear()
        r2 = self._get_no_redirect(
            "http://127.0.0.1:%d/auth/app/session?token=%s" % (self.port, hand))
        self.assertEqual(r2.code, 302)
        self.assertEqual(r2.headers["Location"], "/")
        s, me = self._jget("/me")
        self.assertTrue(me["authenticated"])
        # the same token does not work twice
        self.jar.clear()
        again = self._get_no_redirect(
            "http://127.0.0.1:%d/auth/app/session?token=%s" % (self.port, hand))
        self.assertEqual(again.code, 302)
        self.assertIn("app=again", again.headers["Location"])

    def test_a_forged_token_lands_on_the_door(self):
        r = self._get_no_redirect(
            "http://127.0.0.1:%d/auth/app/session?token=nope" % self.port)
        self.assertEqual(r.code, 302)
        self.assertIn("app=again", r.headers["Location"])

    def test_a_bad_state_is_refused(self):
        self._signup()
        self._wire_fake_provider()
        self._get_no_redirect("http://127.0.0.1:%d/auth/fake/start?link=1" % self.port)
        r = self._get_no_redirect(
            "http://127.0.0.1:%d/auth/fake/callback?code=abc&state=wrong" % self.port)
        self.assertEqual(r.code, 400)
        s, d = self._jget("/auth/linked")
        self.assertEqual(d["linked"], [])


if __name__ == "__main__":
    unittest.main()
