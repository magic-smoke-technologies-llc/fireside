"""One connection, many request threads: the host's actual concurrency shape.

The host is a ThreadingHTTPServer and every handler reaches for the same
App.conn. That is fine for MySQL (MySQLConnection serialises behind a lock) and
it was not fine for SQLite: sqlite3 releases the GIL around a statement, so two
threads could be inside the same connection at once. The failures were quiet and
intermittent, which is why they read as a CI ghost for a day:

  * a session lookup returned no row for a token that was in the table, so the
    page put the sign-in door back over a signed-in reader, and
  * a lookup raised sqlite3.InterfaceError("bad parameter or other API misuse"),
    which the dispatch layer turned into a 503.

Both were observed in a real ui run (a 401 on /last and a 503 on /jobs for one
account, in the same second, while every other request with that same cookie
resolved fine). The tests here pin the shapes that produce them.

What is honest about these: the reserve one fails every time on the old code,
on one core. The two read tests need two threads genuinely inside a statement
at once, so on a single-core box they can pass against the old code too - they
are the guard for CI and for any real deployment, not a trick that reproduces
the ghost on demand. The reproduction that counts is the ui workflow going from
one failure in three runs to green, repeatedly.
"""

import os
import tempfile
import threading
import unittest

from host import accounting, auth, db


def _fresh_conn():
    conn = db.connect(os.path.join(tempfile.mkdtemp(prefix="db-conc-"), "t.db"))
    conn.execute("INSERT INTO users(id,name,created_at) VALUES('u1','u1','now')")
    conn.execute("INSERT OR IGNORE INTO balances(user_id,currency,amount)"
                 " VALUES('u1','USD',10.0)")
    conn.commit()
    return conn


class ConnectionIsShared(unittest.TestCase):

    def test_a_result_keeps_its_rows_while_another_thread_writes(self):
        """The exact shape of the bug: execute, then somebody else writes, then
        read. With a live sqlite cursor the two statements collide on one
        connection; with the rows read out under the lock, they cannot."""
        conn = _fresh_conn()
        conn.execute("INSERT OR IGNORE INTO balances(user_id,currency,amount)"
                     " VALUES('u2','USD',1.0)")
        conn.commit()

        mine = conn.execute("SELECT user_id FROM balances ORDER BY user_id")
        stop = threading.Event()

        def chatter():
            i = 0
            while not stop.is_set():
                i += 1
                conn.execute("SELECT COUNT(*) c FROM balances").fetchone()
                conn.execute("UPDATE balances SET amount=amount WHERE user_id='u2'")
                conn.commit()

        t = threading.Thread(target=chatter)
        t.start()
        try:
            rows = [r["user_id"] for r in mine.fetchall()]
        finally:
            stop.set()
            t.join()
        self.assertEqual(rows, ["u1", "u2"],
                         "a second thread's statement ate this result")

    def test_many_threads_reading_a_row_that_is_there(self):
        """No thread may ever get an empty answer for a row that exists. This is
        the assertion the intermittent 401 broke."""
        conn = _fresh_conn()
        tok = auth.create_session(conn, "u1", "local")
        results, errors = [], []
        barrier = threading.Barrier(10)

        def reader():
            try:
                barrier.wait(timeout=10)
                for _ in range(40):
                    row = conn.execute("SELECT user_id FROM sessions WHERE token=?",
                                       (tok,)).fetchone()
                    results.append(row["user_id"] if row is not None else None)
            except Exception as e:                     # noqa: BLE001 - reported
                errors.append(repr(e))

        threads = [threading.Thread(target=reader) for _ in range(10)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(errors, [], "a shared connection raised under concurrent reads")
        self.assertEqual(len(results), 400)
        self.assertNotIn(None, results, "a live session row came back missing")

    def test_a_failed_reserve_does_not_roll_back_another_threads_pending_write(self):
        """What turned a refusal into a vanished login.

        reserve() used to open BEGIN IMMEDIATE on the shared connection. If any
        other thread had a write pending, that statement raised, and the
        ROLLBACK in its error path threw away the *other* thread's row - the
        session row that had not been committed yet. The cookie was already on
        its way to the client.
        """
        conn = _fresh_conn()
        inserted = threading.Event()
        proceed = threading.Event()
        seen = {}

        def writer():
            conn.execute(
                "INSERT INTO sessions(token,user_id,provider,created_at,expires_at,"
                "sid,user_agent,ip,last_seen)"
                " VALUES('TOK','u1','local',0,9999999999,'s','','',0)")
            inserted.set()
            proceed.wait(10)                  # stay uncommitted while reserve runs
            conn.commit()

        def reserver():
            inserted.wait(10)
            try:
                accounting.reserve(conn, "u1", 999.0)     # more than the balance
            except accounting.Insufficient:
                seen["refused"] = True
            except Exception as e:                        # noqa: BLE001 - reported
                seen["raised"] = repr(e)
            finally:
                proceed.set()

        a, b = threading.Thread(target=reserver), threading.Thread(target=writer)
        a.start()
        b.start()
        a.join(15)
        b.join(15)

        self.assertTrue(seen.get("refused"), "the reserve should have been refused: %r" % seen)
        self.assertIsNone(seen.get("raised"),
                          "a refusal must not turn into a transaction error")
        self.assertIsNotNone(
            conn.execute("SELECT 1 FROM sessions WHERE token='TOK'").fetchone(),
            "a refused reservation rolled back another thread's session row")
        self.assertEqual(
            conn.execute("SELECT COUNT(*) c FROM reservations").fetchone()["c"], 0)


if __name__ == "__main__":
    unittest.main()
