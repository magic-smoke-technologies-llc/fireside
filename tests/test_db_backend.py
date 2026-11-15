import os
import unittest
from unittest.mock import patch

import re

from host import db


class Translate(unittest.TestCase):
    """The SQLite-isms the query layer relies on, rewritten for MySQL."""

    def test_insert_or_ignore(self):
        self.assertEqual(db.translate("INSERT OR IGNORE INTO x VALUES(?)"),
                         "INSERT IGNORE INTO x VALUES(%s)")

    def test_insert_or_replace(self):
        self.assertEqual(db.translate("INSERT OR REPLACE INTO x(a) VALUES(?)"),
                         "REPLACE INTO x(a) VALUES(%s)")

    def test_begin_immediate(self):
        self.assertEqual(db.translate("BEGIN IMMEDIATE"), "START TRANSACTION")

    def test_release_savepoint_gets_its_keyword(self):
        """SQLite accepts `RELEASE x`; MySQL's grammar is `RELEASE SAVEPOINT x`.

        The short form is what add_book writes, so on the live MySQL every
        successful insert ended with a syntax error: the book was written and
        the import still reported the file as broken.
        """
        self.assertEqual(db.translate("RELEASE addbook_ab12cd34ef56"),
                         "RELEASE SAVEPOINT addbook_ab12cd34ef56")

    def test_release_savepoint_already_spelled_out_is_left_alone(self):
        self.assertEqual(db.translate("RELEASE SAVEPOINT sp1"),
                         "RELEASE SAVEPOINT sp1")

    def test_rollback_to_is_legal_on_both_engines(self):
        self.assertEqual(db.translate("ROLLBACK TO addbook_ab12cd34ef56"),
                         "ROLLBACK TO addbook_ab12cd34ef56")

    def test_plain_statement_only_reparameterised(self):
        self.assertEqual(db.translate("SELECT * FROM t WHERE a=? AND b=?"),
                         "SELECT * FROM t WHERE a=%s AND b=%s")

    def test_on_conflict_becomes_on_duplicate_key(self):
        sql = ("INSERT INTO positions(a,b,c) VALUES(?,?,?) "
               "ON CONFLICT(a,b) DO UPDATE SET c=excluded.c")
        self.assertEqual(
            db.translate(sql),
            "INSERT INTO positions(a,b,c) VALUES(%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE c=VALUES(c)")

    def test_on_conflict_rewrites_every_excluded_column(self):
        sql = ("INSERT INTO positions(user_id,book_id,chapter_index,offset_seconds,"
               "updated_at,owner_device_id) VALUES(?,?,?,?,?,?) "
               "ON CONFLICT(user_id,book_id) DO UPDATE SET "
               "chapter_index=excluded.chapter_index,"
               "offset_seconds=excluded.offset_seconds,updated_at=excluded.updated_at")
        self.assertEqual(
            db.translate(sql),
            "INSERT INTO positions(user_id,book_id,chapter_index,offset_seconds,"
            "updated_at,owner_device_id) VALUES(%s,%s,%s,%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE chapter_index=VALUES(chapter_index),"
            "offset_seconds=VALUES(offset_seconds),updated_at=VALUES(updated_at)")

    def test_on_conflict_leaves_non_excluded_rhs_alone(self):
        sql = ("INSERT INTO t(a,b) VALUES(?,?) "
               "ON CONFLICT(a) DO UPDATE SET b=b+1, c=excluded.c")
        self.assertEqual(
            db.translate(sql),
            "INSERT INTO t(a,b) VALUES(%s,%s) "
            "ON DUPLICATE KEY UPDATE b=b+1, c=VALUES(c)")

    def test_on_conflict_keeps_a_function_wrapped_excluded_column(self):
        """COALESCE(NULLIF(excluded.x, ''), tbl.x) is how an empty value is
        refused. Splitting the body on commas used to shred that into invalid
        SQL on every sign-up, because billing.upsert stores ids that way."""
        sql = ("INSERT INTO subscriptions(user_id,reference) VALUES(?,?) "
               "ON CONFLICT(user_id) DO UPDATE SET "
               "reference=COALESCE(NULLIF(excluded.reference, ''), "
               "subscriptions.reference)")
        out = db.translate(sql)
        self.assertNotIn("excluded.", out)
        self.assertNotIn("subscriptions.reference", out)
        self.assertIn("COALESCE(NULLIF(VALUES(reference), ''), reference)", out)



class Savepoints(unittest.TestCase):
    """A savepoint is a SQLite-only feature in this codebase.

    The hosted MySQL runs with autocommit on, so the transaction a SAVEPOINT
    opens is gone before the next statement can use it: SAVEPOINT x then
    RELEASE SAVEPOINT x is 1305 on MySQL 8 with nothing in between. Measured on
    the live server. The helpers below are the only place that decision is
    made, and the failure they prevent lands at the end of a write that has
    already committed.
    """

    class Recording:
        def __init__(self, nested=True):
            self.nested_transactions = nested
            self.sql = []

        def execute(self, sql, params=()):
            self.sql.append(sql)

        def commit(self):
            self.sql.append("COMMIT")

    def test_a_sqlite_connection_has_nested_transactions(self):
        c = db.connect(":memory:")
        try:
            self.assertTrue(db.nested_transactions(c))
        finally:
            c.close()

    def test_a_mysql_connection_says_it_does_not(self):
        c = object.__new__(db.MySQLConnection)
        self.assertFalse(db.nested_transactions(c))

    def test_helpers_write_the_statements_on_sqlite(self):
        c = self.Recording()
        db.savepoint(c, "sp1")
        db.release_savepoint(c, "sp1")
        db.savepoint(c, "sp2")
        db.rollback_to_savepoint(c, "sp2")
        self.assertEqual(c.sql, ["SAVEPOINT sp1", "RELEASE sp1",
                                 "SAVEPOINT sp2",
                                 "ROLLBACK TO sp2", "RELEASE sp2"])

    def test_helpers_write_nothing_where_a_savepoint_does_not_hold(self):
        c = self.Recording(nested=False)
        db.savepoint(c, "sp1")
        db.release_savepoint(c, "sp1")
        db.rollback_to_savepoint(c, "sp1")
        self.assertEqual(c.sql, [], "MySQL must not see a savepoint statement")

    def test_an_unknown_connection_keeps_the_old_behaviour(self):
        """Wrappers and test doubles that predate the flag still get savepoints,
        so a SQLite caller cannot silently lose its nested undo."""
        c = self.Recording()
        del c.nested_transactions
        self.assertTrue(db.nested_transactions(c))


class Backend(unittest.TestCase):
    def test_sqlite_is_default(self):
        self.assertEqual(db.backend_name(""), "sqlite")

    def test_sqlite_url(self):
        self.assertEqual(db.backend_name("sqlite:///tmp/x.db"), "sqlite")

    def test_mysql_urls(self):
        self.assertEqual(db.backend_name("mysql://u:p@h:3306/f"), "mysql")
        self.assertEqual(db.backend_name("mysql+pymysql://u:p@h/f"), "mysql")


class EnvVars(unittest.TestCase):
    """Engine (FIRESIDE_DB_TYPE) and connection (FIRESIDE_DB_CONN) are separate."""

    def test_type_and_connection_are_independent(self):
        with patch.dict(os.environ, {"FIRESIDE_DB_TYPE": "mysql",
                                     "FIRESIDE_DB_CONN": "u:p@h:3306/f"}):
            self.assertEqual(db.db_type(), "mysql")
            self.assertEqual(db.db_conn(), "u:p@h:3306/f")
            self.assertEqual(db.db_url(), "mysql://u:p@h:3306/f")

    def test_mysql_accepts_full_dsn_too(self):
        with patch.dict(os.environ, {"FIRESIDE_DB_TYPE": "mysql",
                                     "FIRESIDE_DB_CONN": "mysql://u:p@h/f"}):
            self.assertEqual(db.db_url(), "mysql://u:p@h/f")

    def test_sqlite_type_with_a_path(self):
        with patch.dict(os.environ, {"FIRESIDE_DB_TYPE": "sqlite",
                                     "FIRESIDE_DB_CONN": "/tmp/f.db"}):
            self.assertEqual(db.db_type(), "sqlite")
            self.assertEqual(db.backend_name(), "sqlite")
            self.assertEqual(db.db_url(), "sqlite:///tmp/f.db")

    def test_sqlite_default_has_no_conn(self):
        with patch.dict(os.environ, {"FIRESIDE_DB_TYPE": "sqlite",
                                     "FIRESIDE_DB_CONN": ""}):
            self.assertEqual(db.db_type(), "sqlite")
            self.assertEqual(db.db_conn(), "")
            self.assertEqual(db.db_url(), "")

    def test_legacy_single_variable_still_works(self):
        with patch.dict(os.environ, {"FIRESIDE_DB": "mysql://u:p@h/f"}):
            os.environ.pop("FIRESIDE_DB_TYPE", None)
            os.environ.pop("FIRESIDE_DB_CONN", None)
            self.assertEqual(db.db_type(), "mysql")
            self.assertEqual(db.db_url(), "mysql://u:p@h/f")

    def test_new_variables_win_over_legacy(self):
        with patch.dict(os.environ, {"FIRESIDE_DB": "mysql://old:p@h/f",
                                     "FIRESIDE_DB_TYPE": "sqlite",
                                     "FIRESIDE_DB_CONN": "/tmp/new.db"}):
            self.assertEqual(db.db_type(), "sqlite")
            self.assertEqual(db.db_url(), "sqlite:///tmp/new.db")


class Schema(unittest.TestCase):
    def test_memory_connection_has_both_new_tables(self):
        c = db.connect(":memory:")
        try:
            c.execute("SELECT * FROM local_accounts").fetchall()
            rows = c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            names = {r["name"] for r in rows}
            self.assertIn("render_cache", names)
            self.assertIn("local_accounts", names)
        finally:
            c.close()

    def test_reserved_word_column_is_usable(self):
        c = db.connect(":memory:")
        try:
            c.execute("INSERT INTO render_cache(`key`,audio_ref) VALUES(?,?)", ("k", "r"))
            r = c.execute("SELECT audio_ref FROM render_cache WHERE `key`=?", ("k",)).fetchone()
            self.assertEqual(r["audio_ref"], "r")
        finally:
            c.close()

    def test_a_blob_reference_is_indexed_on_every_table_that_names_one(self):
        """Removing a book asks "does any row still name this blob?" once per
        blob. Unindexed that is a full scan of the whole shelf per blob, which
        is how a bulk remove used to spend minutes before answering."""
        c = db.connect(":memory:")
        try:
            for table, col in (("chapters", "text_ref"), ("chapters", "audio_ref"),
                               ("renditions", "audio_ref"), ("books", "cover_ref"),
                               ("render_cache", "audio_ref")):
                rows = c.execute("PRAGMA index_list(%s)" % table).fetchall()
                indexed = set()
                for r in rows:
                    for info in c.execute("PRAGMA index_info(%s)" % r["name"]).fetchall():
                        if info["name"] == col:
                            indexed.add(col)
                self.assertIn(col, indexed, "%s.%s must be indexed" % (table, col))
            names = {r["name"] for r in
                     c.execute("PRAGMA index_list(listens)").fetchall()}
            self.assertIn("idx_listens_user_book", names)
        finally:
            c.close()


# Words MySQL will not accept as a bare column name. SQLite takes all of them,
# so a schema that uses one boots on a laptop and kills every MySQL deployment:
# that is exactly what `before` and `after` did on 11-15 (error 1064), taking the
# live app down to a failed revision while the old one kept serving.
MYSQL_RESERVED = {
    "before", "after", "interval", "read", "write", "order", "group", "key",
    "keys", "index", "table", "select", "from", "where", "join", "on", "as",
    "desc", "asc", "limit", "offset", "group_concat", "rank", "system", "range",
    "rows", "current_date", "current_time", "current_timestamp", "usage",
    "signal", "condition", "check", "default", "values", "match",
    "option", "all", "and", "or", "not",
    "in", "is", "like", "replace", "insert", "update", "delete", "create",
    "drop", "alter", "lock", "share", "lines", "load", "column", "columns",
}


class SchemaIsPortable(unittest.TestCase):
    """Every column the schema declares has to be legal on both engines."""

    def _columns(self):
        out = {}
        table = None
        for line in db.SCHEMA.splitlines():
            stripped = line.strip()
            m = re.match(r"CREATE TABLE IF NOT EXISTS\s+(\w+)\s*\(", stripped)
            if m:
                table = m.group(1)
                out.setdefault(table, [])
                continue
            if not table or not stripped or stripped.startswith("--"):
                continue
            if stripped.startswith(")"):
                table = None
                continue
            name = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\s+[A-Za-z]", stripped)
            if name:
                out[table].append(name.group(1))
        return out

    def test_no_column_is_a_mysql_reserved_word(self):
        bad = []
        for table, cols in self._columns().items():
            for c in cols:
                if c.lower() in MYSQL_RESERVED:
                    bad.append("%s.%s" % (table, c))
        assert not bad, "reserved word used as a column: %r" % (bad,)

    def test_the_parser_sees_the_tables_it_is_checking(self):
        """The positive control: a parser that found nothing would pass the
        test above by inspecting an empty schema."""
        cols = self._columns()
        self.assertIn("ledger", cols)
        self.assertIn("admin_audit", cols)
        self.assertIn("balance_before", cols["admin_audit"])


if __name__ == "__main__":
    unittest.main()


class SchemaPortability(unittest.TestCase):
    """The one schema must parse on MySQL as well as SQLite.

    MySQL rejects a literal DEFAULT on TEXT/BLOB/JSON columns (errno 1101), so a
    column like `source_json TEXT DEFAULT ''` boots on SQLite and crash-loops on
    MySQL. This guard keeps the schema honest for both engines.
    """

    def test_text_columns_have_no_literal_default(self):
        bad = re.findall(r"\b(TEXT|BLOB|GEOMETRY|JSON)\s+DEFAULT\b", db.SCHEMA, re.I)
        self.assertEqual(bad, [], "MySQL rejects DEFAULT on %s column(s)" % bad)


class MySQLTls(unittest.TestCase):
    """A hosted MySQL needs TLS; a LAN self-host must not be forced into it.

    Azure Database for MySQL runs with require_secure_transport=ON, so a
    plaintext connection is refused before auth. pymysql only speaks TLS when
    handed an SSL context, so `mysql_ssl_kwargs()` is the single decision point.
    The default is deliberately empty: nothing changes for an existing
    plaintext deployment.
    """

    def tearDown(self):
        os.environ.pop("FIRESIDE_DB_SSL", None)

    def test_unset_keeps_the_plaintext_default(self):
        self.assertEqual(db.mysql_ssl_kwargs(), {})

    def test_require_turns_on_tls(self):
        import ssl
        os.environ["FIRESIDE_DB_SSL"] = "1"
        kw = db.mysql_ssl_kwargs()
        self.assertIsInstance(kw["ssl"], ssl.SSLContext)
        self.assertEqual(kw["ssl"].verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(kw["ssl"].check_hostname)

    def test_noverify_encrypts_without_verifying(self):
        import ssl
        os.environ["FIRESIDE_DB_SSL"] = "noverify"
        ctx = db.mysql_ssl_kwargs()["ssl"]
        self.assertEqual(ctx.verify_mode, ssl.CERT_NONE)
        self.assertFalse(ctx.check_hostname)


class StatementsTest(unittest.TestCase):
    """The schema must split the same way on both engines (2026-11-07)."""

    def test_a_semicolon_in_a_comment_does_not_cut_a_statement(self):
        script = ("-- mirror tables; this semicolon is prose\n"
                  "CREATE TABLE IF NOT EXISTS a (x INT);\n"
                  "CREATE TABLE IF NOT EXISTS b (y INT);")
        got = db.statements(script)
        self.assertEqual(len(got), 2)
        self.assertTrue(got[0].startswith("CREATE TABLE IF NOT EXISTS a"))
        self.assertTrue(got[1].startswith("CREATE TABLE IF NOT EXISTS b"))

    def test_every_schema_statement_starts_with_a_keyword(self):
        # The failure this guards: MySQL got a fragment beginning with prose
        # ("paddle_events is the idempotency") and refused to boot.
        for stmt in db.statements(db.SCHEMA):
            head = stmt.split(None, 1)[0].upper()
            self.assertIn(head, {"CREATE", "ALTER", "INSERT", "PRAGMA", "DROP",
                                 "BEGIN", "UPDATE", "DELETE"}, stmt[:80])

    def test_schema_creates_the_paddle_tables(self):
        joined = " ".join(db.statements(db.SCHEMA))
        for table in ("paddle_customers", "paddle_subscriptions", "paddle_events"):
            self.assertIn("CREATE TABLE IF NOT EXISTS " + table, joined)
