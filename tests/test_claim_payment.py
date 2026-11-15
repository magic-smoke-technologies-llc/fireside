"""A provider event is claimed once, and a claim never outlives a failed credit."""

import os
import tempfile
import unittest

from host import accounting
from host.app import App


class TestClaimPayment(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.app = App(self.tmp, db_path=os.path.join(self.tmp, "db.sqlite"))

    def rows(self, event_id):
        return self.app.conn.execute(
            "SELECT 1 FROM payment_events WHERE id=?", (event_id,)).fetchall()

    def test_first_claim_wins_and_the_second_is_a_duplicate(self):
        self.assertTrue(self.app._claim_payment("evt1", "characters", "u", "evt1", 5))
        self.assertFalse(self.app._claim_payment("evt1", "characters", "u", "evt1", 5))
        self.assertFalse(self.app._claim_payment("  ", "characters", "u", "", 5))

    def test_a_database_error_is_not_reported_as_already_credited(self):
        real = self.app.conn

        class Broken:
            def __getattr__(self, name):
                return getattr(real, name)

            def execute(self, sql, *a):
                if sql.lstrip().startswith("INSERT INTO payment_events"):
                    raise RuntimeError("connection lost")
                return real.execute(sql, *a)

        self.app.conn = Broken()
        with self.assertRaises(RuntimeError):
            self.app._claim_payment("evt2", "characters", "u", "evt2", 5)

    def test_a_failed_credit_releases_the_claim_so_the_retry_can_credit(self):
        def boom():
            raise RuntimeError("ledger down")

        with self.assertRaises(RuntimeError):
            self.app._credit_once("evt3", "characters", "u", "evt3", 5, "t", boom)
        self.assertEqual(self.rows("evt3"), [], "claim must be released")
        done = []
        self.assertTrue(self.app._credit_once(
            "evt3", "characters", "u", "evt3", 5, "t", lambda: done.append(1)))
        self.assertEqual(done, [1])

    def test_a_credited_event_is_not_credited_again(self):
        calls = []
        for _ in range(2):
            self.app._credit_once("evt4", "characters", "u", "evt4", 5, "t",
                                  lambda: calls.append(1))
        self.assertEqual(calls, [1])


if __name__ == "__main__":
    unittest.main()
