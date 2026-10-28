"""The second factor over real HTTP: the door, not the arithmetic.

tests/test_totp.py proves the codes against the RFC's vectors and
tests/test_2fa.py proves the state machine on a bare connection. This one is
about the promise that matters at the seam: a session that has not finished
its factor opens nothing, and the same cookie opens everything once it has.
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

from host import auth, totp
from host.app import App, make_handler
from tests.test_app import READER_BODY, SPEECH_BODY, STORAGE_BODY
from tests.test_pluginhost import write_plugin


def _req(path, obj, port):
    return urllib.request.Request(
        "http://127.0.0.1:%d%s" % (port, path),
        data=json.dumps(obj).encode(),
        headers={"Content-Type": "application/json"})


class SecondFactorOverHTTP(unittest.TestCase):
    def setUp(self):
        # no environment switch here: an admin is forced by the code, so these
        # tests would pass with any environment at all.
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
        return self.browser.open(_req(path, obj, self.port))

    def _jget(self, path):
        with self.browser.open("http://127.0.0.1:%d%s" % (self.port, path)) as r:
            return r.status, json.loads(r.read())

    def _register(self, name):
        with self._post("/auth/local/register",
                        {"username": name, "password": "longenough"}) as r:
            return json.loads(r.read())["user"]

    def _status(self, code):
        try:
            self._jget("/library")
            return 200
        except urllib.error.HTTPError as e:
            return e.code

    def _body(self, code, path="/library"):
        try:
            self.browser.open("http://127.0.0.1:%d%s" % (self.port, path))
            return {}
        except urllib.error.HTTPError as e:
            return json.loads(e.read())

    def _enrol(self, secret_code_offset=0):
        with self._post("/2fa/enroll", {}) as r:
            info = json.loads(r.read())
        code = totp.totp(info["secret"], at=time.time() + secret_code_offset)
        with self._post("/2fa/confirm", {"code": code}) as r:
            out = json.loads(r.read())
        return info, out

    # ---- the admin is forced, everyone else may opt in ----------------
    def test_the_first_account_is_not_walled_on_first_run(self):
        # your own box, your own door: nothing is forced on the only account
        # there is, and the panel says so instead of pretending it is optional.
        self._register("chris")                      # first account = admin
        self.assertEqual(self._status(200), 200)
        s, st = self._jget("/2fa")
        self.assertTrue(st["admin"])
        self.assertFalse(st["enrolled"])
        self.assertTrue(st["deferred"])

    def test_admin_is_held_at_enrolment_once_the_box_is_not_just_yours(self):
        self._register("chris")                      # first account = admin
        self.assertEqual(self._status(200), 200)     # solo: deferred
        self._post("/auth/logout", {})
        self.jar.clear()
        self._register("sam")                        # second account, plain
        self._post("/auth/logout", {})
        self.jar.clear()
        self._post("/auth/local/login",
                   {"username": "chris", "password": "longenough"})
        self.assertEqual(self._status(200), 401, "the skip ends at the second account")
        self.assertEqual(self._body(401).get("twofa"), auth.PENDING_ENROLL)
        _info, out = self._enrol()
        self.assertEqual(len(out["recovery"]), totp.RECOVERY_CODES)
        # the same session is full now, no second login
        self.assertEqual(self._status(200), 200)
        s, st = self._jget("/2fa")
        self.assertEqual(st["twofa"], "")
        self.assertTrue(st["enrolled"])
        self.assertFalse(st["deferred"])

    def test_plain_user_needs_no_factor_until_they_enrol(self):
        self._register("chris")                      # admin, forced
        self._enrol()                                # ...so it has to enrol
        self.jar.clear()
        self._post("/auth/local/register", {"username": "sam", "password": "longenough"})
        self.assertEqual(self._status(200), 200, "optional means optional")
        s, st = self._jget("/2fa")
        self.assertFalse(st["enrolled"])
        self.assertFalse(st["required"])
        self.assertEqual(st["twofa"], "")
        self._enrol()                                # now sam opts in
        self.jar.clear()
        self._post("/auth/local/login", {"username": "sam", "password": "longenough"})
        self.assertEqual(self._body(401).get("twofa"), auth.PENDING_CODE)

    def test_tenant_login_after_enrolment_is_held_for_a_code(self):
        self._register("chris")
        self._enrol()
        self.jar.clear()
        self._register("sam")                        # second account, plain
        with self._post("/2fa/enroll", {}) as r:
            info = json.loads(r.read())
        with self._post("/2fa/confirm",
                        {"code": totp.totp(info["secret"])}) as r:
            self.assertEqual(r.status, 200)
        # sign out and back in: now the factor is owed
        self._post("/auth/logout", {})
        self.jar.clear()
        self._post("/auth/local/login", {"username": "sam", "password": "longenough"})
        self.assertEqual(self._status(200), 401)
        self.assertEqual(self._body(401).get("twofa"), auth.PENDING_CODE)
        # nothing else is reachable with a half-session
        self.assertEqual(self._body(401, "/sessions").get("twofa"),
                         auth.PENDING_CODE)
        with self._post("/2fa/verify",
                        {"code": totp.totp(info["secret"],
                                           at=time.time() + totp.STEP)}) as r:
            self.assertEqual(r.status, 200)
        self.assertEqual(self._status(200), 200)

    def test_wrong_code_keeps_the_door_shut(self):
        self._register("chris")
        self._enrol()
        self.jar.clear()
        self._register("sam")
        with self._post("/2fa/enroll", {}) as r:
            info = json.loads(r.read())
        self._post("/2fa/confirm", {"code": totp.totp(info["secret"])})
        self._post("/auth/logout", {})
        self.jar.clear()
        self._post("/auth/local/login", {"username": "sam", "password": "longenough"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._post("/2fa/verify", {"code": "000000"})
        self.assertEqual(cm.exception.code, 401)
        self.assertEqual(self._status(200), 401)

    def test_a_recovery_code_gets_you_in_once(self):
        self._register("chris")
        _info, out = self._enrol()
        codes = out["recovery"]
        self._post("/auth/logout", {})
        self.jar.clear()
        self._post("/auth/local/login", {"username": "chris", "password": "longenough"})
        self.assertEqual(self._status(200), 401)
        with self._post("/2fa/verify", {"recovery": codes[0]}) as r:
            self.assertEqual(r.status, 200)
            self.assertEqual(json.loads(r.read())["recovery_left"],
                             totp.RECOVERY_CODES - 1)
        self.assertEqual(self._status(200), 200)

    def test_an_admin_cannot_switch_it_off(self):
        self._register("chris")
        self._enrol()
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._post("/2fa/disable", {"code": "123456"})
        self.assertEqual(cm.exception.code, 403)

    def test_replacing_a_live_factor_needs_the_live_factor(self):
        # Without this, a stolen session could point the second factor at its
        # own phone and keep the archive. The refusal must also leave the real
        # secret alone.
        uid = self._register("chris")
        info, _out = self._enrol()
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._post("/2fa/enroll", {})
        self.assertEqual(cm.exception.code, 401)
        self.assertTrue(json.loads(cm.exception.read()).get("needs_proof"))
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._post("/2fa/enroll", {"code": "000000"})
        self.assertEqual(cm.exception.code, 401)
        # a code from the next step still opens it: the refusal touched nothing
        still = totp.totp(info["secret"], at=time.time() + 30)
        self.assertTrue(auth.check_code(self.app.conn, uid, still))

    def test_change_the_authenticator_with_a_current_code(self):
        uid = self._register("chris")
        info, _out = self._enrol()
        code = totp.totp(info["secret"], at=time.time() + 30)
        with self._post("/2fa/enroll", {"code": code}) as r:
            new = json.loads(r.read())
        self.assertTrue(new["changing"])
        self.assertNotEqual(new["secret"], info["secret"])
        # the old app is dead the moment the secret is replaced
        self.assertFalse(auth.check_code(
            self.app.conn, uid, totp.totp(info["secret"], at=time.time() + 30)))
        # the fresh secret is live only after it is proven
        with self._post("/2fa/confirm", {
                "code": totp.totp(new["secret"], at=time.time() + 30)}) as r:
            out = json.loads(r.read())
        self.assertEqual(len(out["recovery"]), totp.RECOVERY_CODES)

    def test_lost_phone_change_goes_through_a_recovery_code(self):
        self._register("chris")
        _info, out = self._enrol()
        with self._post("/2fa/enroll", {"recovery": out["recovery"][0]}) as r:
            new = json.loads(r.read())
        self.assertTrue(new["changing"])
        self.assertTrue(new["secret"])

    def test_an_admin_cannot_switch_it_off_even_with_a_good_code(self):
        # an admin account always carries a factor, so this route has no way to
        # quietly undo that. Refusing is not a lockout: change, recovery codes
        # and tools/reset_2fa.py all keep a way in.
        uid = self._register("chris")
        info, _out = self._enrol()
        s, st = self._jget("/2fa")
        self.assertTrue(st["required"])
        self.assertFalse(st["can_disable"])
        code = totp.totp(info["secret"], at=time.time() + 30)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("/2fa/disable", {"code": code})
        self.assertEqual(ctx.exception.code, 403)
        self.assertTrue(auth.enrolled(self.app.conn, uid))

    def test_a_plain_user_can_switch_their_own_factor_off(self):
        self._register("chris")
        self._enrol()
        self.jar.clear()
        uid = self._register("sam")
        info, _out = self._enrol()
        s, st = self._jget("/2fa")
        self.assertFalse(st["required"])
        self.assertTrue(st["can_disable"])
        code = totp.totp(info["secret"], at=time.time() + 30)
        with self._post("/2fa/disable", {"code": code}) as r:
            self.assertEqual(r.status, 200)
        self.assertFalse(auth.enrolled(self.app.conn, uid))

    def test_a_half_session_can_still_sign_out(self):
        self._register("chris")
        with self._post("/auth/logout", {}) as r:
            self.assertEqual(r.status, 200)
        self.jar.clear()
        self._post("/auth/local/login", {"username": "chris", "password": "longenough"})
        # no factor finished, and yet signing out works instead of trapping them
        with self._post("/auth/logout", {}) as r:
            self.assertEqual(r.status, 200)


if __name__ == "__main__":
    unittest.main()
