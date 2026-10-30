"""The second factor, at the fold.

The point of these tests is not "does TOTP work" (tests/test_totp.py proves
that against the RFC's own vectors). It is that a login route never has to
know about 2FA: the mint decides, and the resolve enforces. So each case here
mints a session the way a real route does - one call, no flag - and then asks
whether that session opens the door.
"""

import os
import time
import unittest

from host import auth, db, totp


class TwoFactorAtTheFold(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        # the first local account is promoted to admin by the registration
        # route, so the plain user has to be a second account.
        self.root = auth.local_register(self.conn, "chris", "hunter2hunter2")
        self.uid = auth.local_register(self.conn, "reader", "hunter2hunter2")
        self.assertTrue(auth.is_admin(self.conn, self.root))
        self.assertFalse(auth.is_admin(self.conn, self.uid))

    def tearDown(self):
        self.conn.close()

    def _enroll(self):
        """Enrol the account and return a helper that produces live codes."""
        info = auth.begin_enroll(self.conn, self.uid)
        self.assertIsNone(auth.confirm_enroll(self.conn, self.uid, "000000"))
        codes = auth.confirm_enroll(
            self.conn, self.uid, totp.totp(info["secret"], at=time.time()))
        self.assertTrue(codes)
        return info["secret"], codes

    def _fresh(self, secret):
        """The next step's code: enrolment already spent this one, and the
        replay guard is the reason it can never be spent twice."""
        return totp.totp(secret, at=time.time() + totp.STEP)

    # ---- the mint decides by itself ------------------------------------
    def test_plain_user_mints_a_live_session(self):
        tok = auth.create_session(self.conn, self.uid, "local")
        self.assertEqual(auth.session_user(self.conn, tok), self.uid)

    def test_enrolled_user_mints_a_half_session(self):
        self._enroll()
        tok = auth.create_session(self.conn, self.uid, "local")
        self.assertIsNone(auth.session_user(self.conn, tok),
                          "a pending session must not open anything")
        self.assertEqual(auth.pending_state(self.conn, tok), auth.PENDING_CODE)

    def test_an_admin_is_always_asked_for_a_factor(self):
        # no switch can put an admin account on one key: the admin can read
        # every other account on the box. The half-session still lets them
        # enrol, so being required is not being shut out.
        tok = auth.create_session(self.conn, self.root, "local")
        self.assertIsNone(auth.session_user(self.conn, tok))
        self.assertEqual(auth.pending_state(self.conn, tok), auth.PENDING_ENROLL)

    def test_the_only_account_on_the_box_is_not_held_at_enrolment(self):
        # first run on your own box: the account you just made does not get a
        # wall in front of the app it is the first to see (Chris, 10-28).
        solo = db.connect(":memory:")
        self.addCleanup(solo.close)
        mine = auth.local_register(solo, "owner", "hunter2hunter2")
        self.assertTrue(auth.is_admin(solo, mine))
        tok = auth.create_session(solo, mine, "local")
        self.assertEqual(auth.session_user(solo, tok), mine)
        self.assertEqual(auth.pending_state(solo, tok), "")

    def test_that_skip_ends_at_the_second_account(self):
        # the exemption is self-closing: it is not a flag, and once the box has
        # somebody else on it the admin rule applies again at the next login.
        solo = db.connect(":memory:")
        self.addCleanup(solo.close)
        mine = auth.local_register(solo, "owner", "hunter2hunter2")
        tok = auth.create_session(solo, mine, "local")
        self.assertEqual(auth.session_user(solo, tok), mine)
        other = auth.local_register(solo, "friend", "hunter2hunter2")
        self.assertFalse(auth.is_admin(solo, other))
        self.assertTrue(auth.bootstrap_skip(solo, mine) is False)
        tok = auth.create_session(solo, mine, "local")
        self.assertIsNone(auth.session_user(solo, tok))
        self.assertEqual(auth.pending_state(solo, tok), auth.PENDING_ENROLL)

    def test_the_skip_is_a_deferral_not_a_drop(self):
        # the role rule still says an admin carries a factor: that is what
        # stops an admin turning it off, wall or no wall.
        solo = db.connect(":memory:")
        self.addCleanup(solo.close)
        mine = auth.local_register(solo, "owner", "hunter2hunter2")
        self.assertTrue(auth.must_have_factor(solo, mine))
        self.assertFalse(auth.factor_required(solo, mine))

    def test_a_plain_user_is_never_forced_and_no_env_var_pretends_otherwise(self):
        # FIRESIDE_REQUIRE_2FA used to gate admins and no longer gates anything.
        # A setting that is read and ignored is worse than no setting: it reads
        # green in a deploy check while the behaviour is someone else's decision.
        tok = auth.create_session(self.conn, self.uid, "local")
        self.assertEqual(auth.session_user(self.conn, tok), self.uid)
        os.environ["FIRESIDE_REQUIRE_2FA"] = "1"
        self.addCleanup(os.environ.pop, "FIRESIDE_REQUIRE_2FA", None)
        tok = auth.create_session(self.conn, self.uid, "local")
        self.assertEqual(auth.session_user(self.conn, tok), self.uid)
        tok = auth.create_session(self.conn, self.root, "local")
        self.assertIsNone(auth.session_user(self.conn, tok))

    def test_admin_without_enrolment_is_told_to_enrol(self):
        tok = auth.create_session(self.conn, self.root, "local")
        self.assertIsNone(auth.session_user(self.conn, tok))
        self.assertEqual(auth.pending_state(self.conn, tok), auth.PENDING_ENROLL)
        self.assertEqual(auth.pending_state(self.conn, tok), auth.PENDING_ENROLL)
        # and once the admin has a confirmed secret, it asks for a code instead
        info = auth.begin_enroll(self.conn, self.root)
        auth.confirm_enroll(self.conn, self.root, totp.totp(info["secret"]))
        tok = auth.create_session(self.conn, self.root, "local")
        self.assertEqual(auth.pending_state(self.conn, tok), auth.PENDING_CODE)

    # ---- the half-session is time-limited, unlike a real one -----------
    def test_pending_session_expires_on_its_own_clock(self):
        self._enroll()
        tok = auth.create_session(self.conn, self.uid, "local")
        row = self.conn.execute("SELECT expires_at FROM sessions WHERE token=?",
                                (tok,)).fetchone()
        left = int(row["expires_at"]) - int(time.time())
        self.assertLessEqual(left, auth.PENDING_TTL)
        self.assertLess(left, auth.SESSION_TTL)
        # and finishing the factor hands back the full month
        secret, _codes = self._enroll()
        tok = auth.create_session(self.conn, self.uid, "local")
        self.assertEqual(auth.satisfy_factor(self.conn, tok), self.uid)
        row = self.conn.execute("SELECT expires_at FROM sessions WHERE token=?",
                                (tok,)).fetchone()
        self.assertGreater(int(row["expires_at"]) - int(time.time()),
                           auth.PENDING_TTL)

    # ---- finishing the factor -----------------------------------------
    def test_right_code_promotes_the_same_session(self):
        secret, _ = self._enroll()
        tok = auth.create_session(self.conn, self.uid, "local")
        code = self._fresh(secret)
        self.assertTrue(auth.check_code(self.conn, self.uid, code))
        self.assertEqual(auth.satisfy_factor(self.conn, tok), self.uid)
        self.assertEqual(auth.session_user(self.conn, tok), self.uid)

    def test_wrong_code_does_not_promote(self):
        self._enroll()
        tok = auth.create_session(self.conn, self.uid, "local")
        self.assertFalse(auth.check_code(self.conn, self.uid, "000000"))
        self.assertIsNone(auth.session_user(self.conn, tok))

    def test_same_code_twice_is_refused(self):
        secret, _ = self._enroll()
        code = self._fresh(secret)
        self.assertTrue(auth.check_code(self.conn, self.uid, code))
        self.assertFalse(auth.check_code(self.conn, self.uid, code),
                         "the same code must not work twice")

    def test_unconfirmed_secret_gates_nothing(self):
        auth.begin_enroll(self.conn, self.uid)      # scanned nothing yet
        tok = auth.create_session(self.conn, self.uid, "local")
        self.assertEqual(auth.session_user(self.conn, tok), self.uid,
                         "an abandoned enrolment must not lock the account out")

    def test_reenrolling_kills_the_unconfirmed_secret(self):
        first = auth.begin_enroll(self.conn, self.uid)
        second = auth.begin_enroll(self.conn, self.uid)
        self.assertNotEqual(first["secret"], second["secret"])
        self.assertIsNone(auth.confirm_enroll(
            self.conn, self.uid, totp.totp(first["secret"])))

    # ---- recovery codes -----------------------------------------------
    def test_recovery_code_is_one_shot(self):
        _secret, codes = self._enroll()
        self.assertEqual(len(codes), totp.RECOVERY_CODES)
        self.assertTrue(auth.spend_recovery_code(self.conn, self.uid, codes[0]))
        self.assertFalse(auth.spend_recovery_code(self.conn, self.uid, codes[0]))
        self.assertTrue(auth.spend_recovery_code(self.conn, self.uid, codes[1]))
        self.assertEqual(auth.recovery_code_left(self.conn, self.uid),
                         totp.RECOVERY_CODES - 2)

    def test_recovery_code_tolerates_how_it_was_copied(self):
        _secret, codes = self._enroll()
        messy = "  " + codes[0].upper() + " "
        self.assertTrue(auth.spend_recovery_code(self.conn, self.uid, messy))

    def test_recovery_codes_are_not_stored_in_the_clear(self):
        _secret, codes = self._enroll()
        rows = self.conn.execute("SELECT code_hash FROM twofa_recovery"
                                 " WHERE user_id=?", (self.uid,)).fetchall()
        stored = [r["code_hash"] for r in rows]
        self.assertEqual(len(stored), totp.RECOVERY_CODES)
        self.assertNotIn(codes[0], stored)

    # ---- turning it off -------------------------------------------------
    def test_disable_removes_secret_and_codes(self):
        self._enroll()
        auth.disable_2fa(self.conn, self.uid)
        self.assertFalse(auth.enrolled(self.conn, self.uid))
        self.assertEqual(auth.recovery_code_left(self.conn, self.uid), 0)
        tok = auth.create_session(self.conn, self.uid, "local")
        self.assertEqual(auth.session_user(self.conn, tok), self.uid)


if __name__ == "__main__":
    unittest.main()
