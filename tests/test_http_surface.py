"""The HTTP surface itself: dispatch, cookies, redirects, and the public doors.

The route handlers are covered by their own tests; this file covers the layer
above them - the 404 for an unknown path, the 400 for a body that is not JSON,
the 500 when a handler throws, the HTML and raw-byte response shapes, and the
cookie/redirect behaviour of sign-in and sign-out. It boots the real server.
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

from host import auth, payments
from host.app import App, make_handler
from tests.test_app import READER_BODY, SPEECH_BODY, STORAGE_BODY
from tests.test_pluginhost import write_plugin

PROVIDERS = {
    "fake": {
        "label": "Fake Door",
        "authorize": "https://example.com/authorize",
        "token": "https://example.com/token",
        "identity": "https://example.com/me",
        "scope": "email",
        "id_env": ("FIRESIDE_OAUTH_FAKE_ID", "FIRESIDE_OAUTH_FAKE_SECRET"),
    }
}


class Base(unittest.TestCase):
    require_auth = False

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        root = os.path.join(self.tmp, "plugins")
        write_plugin(root, "reader", "fake", order=10, body=READER_BODY)
        write_plugin(root, "speech", "fake", body=SPEECH_BODY)
        write_plugin(root, "storage", "mem", body=STORAGE_BODY)
        self.app = App(root, db_path=os.path.join(self.tmp, "db.sqlite"),
                       storage_id="mem", require_auth=self.require_auth)
        self.app.auth = auth.Auth(self.app.conn, providers=PROVIDERS, local=True)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.jar = http.cookiejar.CookieJar()
        nop = urllib.request.ProxyHandler({})

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **kw):
                return None

        self.browser = urllib.request.build_opener(
            nop, urllib.request.HTTPCookieProcessor(self.jar))
        self.plain = urllib.request.build_opener(nop, NoRedirect())
        os.environ["FIRESIDE_OAUTH_FAKE_ID"] = "cid"
        os.environ["FIRESIDE_OAUTH_FAKE_SECRET"] = "sec"

    def tearDown(self):
        self.httpd.shutdown()
        for k in ("FIRESIDE_OAUTH_FAKE_ID", "FIRESIDE_OAUTH_FAKE_SECRET",
                  "FIRESIDE_BASE_URL", "FIRESIDE_COMPANY"):
            os.environ.pop(k, None)
        payments.REGISTRY.clear()

    def url(self, path):
        return "http://127.0.0.1:%d%s" % (self.port, path)

    def get(self, path, follow=True):
        op = self.browser if follow else self.plain
        return op.open(self.url(path))

    def post(self, path, obj=None, raw=None, headers=None):
        body = raw if raw is not None else (json.dumps(obj or {}).encode() if obj is not None else b"")
        h = {"Content-Type": "application/json"}
        h.update(headers or {})
        req = urllib.request.Request(self.url(path), data=body, headers=h)
        return self.browser.open(req)

    def post_no_follow(self, path, obj=None):
        req = urllib.request.Request(self.url(path), data=json.dumps(obj or {}).encode(),
                                     headers={"Content-Type": "application/json"})
        return self.plain.open(req)

    def status(self, fn, *a, **kw):
        try:
            with fn(*a, **kw) as r:
                return r.status, r
        except urllib.error.HTTPError as e:
            return e.code, e


class TestDispatch(Base):
    def test_unknown_path_is_404(self):
        code, _ = self.status(self.get, "/nope")
        self.assertEqual(code, 404)
        code, _ = self.status(self.post, "/nope", {})
        self.assertEqual(code, 404)

    def test_bad_json_body_is_400(self):
        code, _ = self.status(self.post, "/import", raw=b"{not json")
        self.assertEqual(code, 400)

    def test_a_handler_that_throws_is_500_with_the_reason(self):
        def boom(q, body):
            raise RuntimeError("kaboom")

        self.app.health = boom
        code, r = self.status(self.get, "/health")
        self.assertEqual(code, 500)
        self.assertIn("kaboom", r.read().decode())

    def test_a_session_lookup_that_throws_is_503_and_logged(self):
        """The lookup runs before routing, outside any handler's try, so a raise
        there used to close the connection with no status and no traceback: a
        client sees "logged out", an operator sees nothing. It is a server fault
        and has to say so, out loud."""
        import contextlib
        import io
        from unittest import mock

        noise = io.StringIO()
        with mock.patch.object(auth, "session_user", side_effect=RuntimeError("db gone")):
            with contextlib.redirect_stderr(noise):
                code, r = self.status(self.get, "/library")
        self.assertEqual(code, 503)
        body = json.loads(r.read())
        self.assertIn("db gone", body["error"])
        self.assertIn("db gone", noise.getvalue())      # and it went in the log too

    def test_html_response_shape(self):
        r = self.get("/privacy")
        self.assertEqual(r.headers.get("Content-Type"), "text/html; charset=utf-8")
        self.assertIn("<!doctype", r.read().decode().lower())
        self.assertEqual(r.headers.get("Cache-Control"), "no-store")

    def test_raw_audio_response_shape(self):
        r = self.post("/speak", {"text": "hi", "provider": "fake", "user": "u1"})
        self.assertEqual(r.status, 200)
        self.assertEqual(r.headers.get("Content-Type"), "audio/mpeg")
        self.assertTrue(r.read())

    def test_bearer_token_counts_as_a_session(self):
        s = auth.local_register(self.app.conn, "bearer", "longenough", "")

        class H:
            SESSION_COOKIE = auth.SESSION_COOKIE

        tok = auth.create_session(self.app.conn, s, "local")
        self.app.require_auth = True
        req = urllib.request.Request(self.url("/me"),
                                     headers={"Authorization": "Bearer " + tok})
        with urllib.request.urlopen(req) as r:
            self.assertEqual(r.status, 200)
            self.assertTrue(json.loads(r.read())["authenticated"])


class TestAuthDoors(Base):
    def test_start_redirects_and_sets_a_state_cookie(self):
        code, r = self.status(self.get, "/auth/fake/start", False)
        self.assertEqual(code, 302)
        self.assertIn("state=", r.headers.get("Location"))
        cookies = ",".join(r.headers.get_all("Set-Cookie") or [])
        self.assertIn(auth.STATE_COOKIE, cookies)

    def test_secure_cookie_when_base_url_is_https(self):
        os.environ["FIRESIDE_BASE_URL"] = "https://readfireside.example"
        code, r = self.status(self.get, "/auth/fake/start", False)
        cookies = ",".join(r.headers.get_all("Set-Cookie") or [])
        self.assertIn("Secure", cookies)

    def test_unknown_provider_is_404(self):
        code, _ = self.status(self.get, "/auth/nope/start", False)
        self.assertEqual(code, 404)

    def test_linking_without_a_session_is_401(self):
        code, _ = self.status(self.get, "/auth/fake/start?link=1", False)
        self.assertEqual(code, 401)

    def test_callback_without_state_is_400(self):
        code, _ = self.status(self.get, "/auth/fake/callback?code=x", False)
        self.assertEqual(code, 400)

    def test_callback_with_an_error_is_400(self):
        code, _ = self.status(self.get, "/auth/fake/callback?error=denied", False)
        self.assertEqual(code, 400)

    def test_logout_clears_the_cookie_and_redirects(self):
        code, r = self.status(self.post_no_follow, "/auth/logout", {})
        self.assertEqual(code, 302)
        self.assertIn("Max-Age=0", ",".join(r.headers.get_all("Set-Cookie") or []))

    def test_register_signs_in_and_survives_bad_input(self):
        code, r = self.status(self.post, "/auth/local/register", raw=b"{bad")
        self.assertEqual(code, 400)
        r = self.post("/auth/local/register", {"username": "surf", "password": "longenough"})
        self.assertEqual(r.status, 200)
        self.assertIn(auth.SESSION_COOKIE, ",".join(r.headers.get_all("Set-Cookie") or []))
        code, _ = self.status(self.post, "/auth/local/register",
                              {"username": "surf", "password": "longenough"})
        self.assertEqual(code, 401)

    def test_password_change_needs_a_session(self):
        code, _ = self.status(self.post, "/account/password",
                              {"current": "longenough", "password": "anotherpass"})
        self.assertEqual(code, 401)

    def test_password_change_swaps_the_password(self):
        self.post("/auth/local/register", {"username": "surf", "password": "longenough"})
        r = self.post("/account/password",
                      {"current": "longenough", "password": "anotherpass"})
        self.assertEqual(r.status, 200)
        body = json.loads(r.read())
        self.assertTrue(body["ok"])
        self.assertEqual(body["username"], "surf")
        # the account still works with the new password
        self.assertEqual(auth.local_login(self.app.conn, "surf", "anotherpass"),
                         "local:surf")

    def test_password_change_refuses_a_wrong_current_one(self):
        self.post("/auth/local/register", {"username": "surf", "password": "longenough"})
        code, r = self.status(self.post, "/account/password",
                              {"current": "not-it-at-all", "password": "anotherpass"})
        self.assertEqual(code, 400)
        self.assertIn("current password", json.loads(r.read())["error"])

    def test_password_change_shows_has_password_on_me(self):
        self.post("/auth/local/register", {"username": "surf", "password": "longenough"})
        me = json.loads(self.get("/me").read())
        self.assertTrue(me["has_password"])

    def test_claim_check_and_confirm_reject_a_bad_token(self):
        code, r = self.status(self.get, "/auth/claim?token=nope")
        self.assertEqual(code, 410)
        code, r = self.status(self.post, "/auth/claim/confirm",
                              {"claim": "nope", "password": "longenough"})
        self.assertEqual(code, 401)

    def test_forgot_says_nothing_about_who_has_an_account(self):
        r = self.post("/auth/forgot", {"email": "nobody@example.com"})
        self.assertEqual(r.status, 200)
        self.assertIn("mail", json.loads(r.read()))

    def test_forgot_rejects_bad_json(self):
        code, _ = self.status(self.post, "/auth/forgot", raw=b"{bad")
        self.assertEqual(code, 400)

    def test_reset_rejects_a_bad_token(self):
        code, _ = self.status(self.post, "/auth/reset", {"token": "nope",
                                                         "password": "longenough"})
        self.assertEqual(code, 401)

    def test_webhook_route_reaches_the_provider(self):
        code, _ = self.status(self.post, "/webhook/nope", raw=b"{}")
        self.assertEqual(code, 404)

    def test_reset_rejects_bad_json(self):
        code, _ = self.status(self.post, "/auth/reset", raw=b"{bad")
        self.assertEqual(code, 400)

    def test_claim_confirm_rejects_bad_json(self):
        code, _ = self.status(self.post, "/auth/claim/confirm", raw=b"{bad")
        self.assertEqual(code, 400)


class TestAuthGate(Base):
    require_auth = True

    def test_private_route_needs_a_session_public_one_does_not(self):
        code, _ = self.status(self.get, "/library")
        self.assertEqual(code, 401)
        code, r = self.status(self.get, "/privacy")
        self.assertEqual(code, 200)


class TestHandlerHelpers(unittest.TestCase):
    def test_truthy(self):
        from host.app import _truthy
        for v in ("1", "true", "YES", " on "):
            self.assertTrue(_truthy(v))
        for v in ("", "0", "no", None):
            self.assertFalse(_truthy(v))


if __name__ == "__main__":
    unittest.main()
