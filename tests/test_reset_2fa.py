"""The out-of-band rescue, and the rule surviving it.

An admin always carries a factor, so there is no route that turns it off. When
the phone AND the recovery codes are gone, the only honest out is one that needs
a credential the web app does not give you: the container. That is
tools/reset_2fa.py. These tests pin the two things that make it a rescue rather
than a hole - it clears the factor, and the next login must enrol again.
"""

import os
import tempfile
import time
import unittest

import host.auth as auth
import host.db as db
import host.totp as totp
import tools.reset_2fa as reset


class ResetSecondFactor(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "db.sqlite")
        self.conn = db.connect(self.path)
        self.root = auth.local_register(self.conn, "chris", "hunter2hunter2")
        self.assertTrue(auth.is_admin(self.conn, self.root))
        info = auth.begin_enroll(self.conn, self.root)
        self.codes = auth.confirm_enroll(self.conn, self.root,
                                         totp.totp(info["secret"], at=time.time()))
        self.assertTrue(auth.enrolled(self.conn, self.root))

    def tearDown(self):
        self.conn.close()

    def _run(self, *args):
        return reset.main(list(args) + ["--db", self.path])

    def test_it_clears_the_factor_and_the_sessions(self):
        tok = auth.create_session(self.conn, self.root, "local")
        self.assertEqual(self._run("--user", "chris", "--confirm", "chris"), 0)
        self.assertFalse(auth.enrolled(self.conn, self.root))
        self.assertEqual(auth.session_user(self.conn, tok), None,
                         "a rescue that leaves the stolen sessions alive is not one")
        self.assertEqual(auth.recovery_code_left(self.conn, self.root), 0)

    def test_the_next_login_must_enrol_again(self):
        self.assertEqual(self._run("--user", "chris", "--confirm", "chris"), 0)
        tok = auth.create_session(self.conn, self.root, "local")
        self.assertIsNone(auth.session_user(self.conn, tok))
        self.assertEqual(auth.pending_state(self.conn, tok), auth.PENDING_ENROLL)

    def test_a_wrong_confirmation_does_nothing(self):
        self.assertEqual(self._run("--user", "chris", "--confirm", "sam"), 2)
        self.assertTrue(auth.enrolled(self.conn, self.root))

    def test_an_unknown_account_is_an_error_not_a_silent_success(self):
        self.assertEqual(self._run("--user", "sam", "--confirm", "sam"), 1)

    def test_it_finds_the_account_by_id_too(self):
        self.assertEqual(self._run("--user", self.root, "--confirm", self.root), 0)
        self.assertFalse(auth.enrolled(self.conn, self.root))


if __name__ == "__main__":
    unittest.main()
