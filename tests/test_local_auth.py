import unittest

from host import auth, db


class LocalAccounts(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")

    def tearDown(self):
        self.conn.close()

    def test_register_then_login(self):
        uid = auth.local_register(self.conn, "Chris", "hunter2hunter2")
        self.assertEqual(uid, "local:chris")
        self.assertEqual(auth.local_login(self.conn, "chris", "hunter2hunter2"), uid)

    def test_wrong_password_rejected(self):
        auth.local_register(self.conn, "chris", "hunter2hunter2")
        with self.assertRaises(auth.AuthError):
            auth.local_login(self.conn, "chris", "nope")

    def test_unknown_user_rejected(self):
        with self.assertRaises(auth.AuthError):
            auth.local_login(self.conn, "nobody", "hunter2hunter2")

    def test_duplicate_username_rejected(self):
        auth.local_register(self.conn, "chris", "hunter2hunter2")
        with self.assertRaises(auth.AuthError):
            auth.local_register(self.conn, "chris", "another-pass")

    def test_short_password_rejected(self):
        with self.assertRaises(auth.AuthError):
            auth.local_register(self.conn, "chris", "short")

    def test_bad_username_rejected(self):
        with self.assertRaises(auth.AuthError):
            auth.local_register(self.conn, "no spaces", "hunter2hunter2")

    def test_email_stored_normalized(self):
        auth.local_register(self.conn, "chris", "hunter2hunter2",
                            email="  Chris@Example.COM ")
        row = self.conn.execute(
            "SELECT email FROM local_accounts WHERE username=?", ("chris",)).fetchone()
        self.assertEqual(row["email"], "chris@example.com")

    def test_malformed_email_rejected(self):
        with self.assertRaises(auth.AuthError):
            auth.local_register(self.conn, "chris", "hunter2hunter2", email="not-an-email")

    def test_email_optional_for_old_callers(self):
        uid = auth.local_register(self.conn, "chris", "hunter2hunter2")
        self.assertEqual(uid, "local:chris")

    def test_a_hosted_server_can_refuse_an_account_with_no_email(self):
        # A social login can only find an account by its address. An account
        # with none is invisible to that door, so the same person signing in
        # with Google lands on a SECOND account and nothing errors (#159).
        with self.assertRaises(auth.AuthError) as ctx:
            auth.local_register(self.conn, "chris", "hunter2hunter2",
                                require_email=True)
        self.assertIn("email", str(ctx.exception))
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM local_accounts WHERE username=?", ("chris",)).fetchone())

    def test_require_email_still_accepts_a_good_address(self):
        uid = auth.local_register(self.conn, "chris", "hunter2hunter2",
                                  email="chris@example.com", require_email=True)
        self.assertEqual(uid, "local:chris")

    def test_require_email_does_not_relax_the_shape_check(self):
        with self.assertRaises(auth.AuthError):
            auth.local_register(self.conn, "chris", "hunter2hunter2",
                                email="not-an-email", require_email=True)

    def test_account_for_email_finds_local(self):
        auth.local_register(self.conn, "chris", "hunter2hunter2", email="chris@example.com")
        self.assertEqual(auth.account_for_email(self.conn, "CHRIS@example.com"),
                         "local:chris")
        self.assertIsNone(auth.account_for_email(self.conn, "nobody@example.com"))
        # an unverified address is not a merge key, only a "who is this?" key
        self.assertIsNone(auth.account_for_verified_email(self.conn, "chris@example.com"))

    def test_local_listed_when_enabled(self):
        a = auth.Auth(self.conn)
        self.assertTrue(any(p["id"] == "local" for p in a.providers()))

    def test_local_hidden_when_disabled(self):
        a = auth.Auth(self.conn, local=False)
        self.assertFalse(any(p["id"] == "local" for p in a.providers()))


if __name__ == "__main__":
    unittest.main()
