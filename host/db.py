"""Host state: the mutable records (users, library, jobs, ledger, positions).

Blobs (source bytes, rendered audio) live in the storage backend and are named
by content hash. Everything mutable and queryable lives here, in the data dir.

One shape, two engines. The host speaks one connection dialect - `?`
placeholders, rows addressed by column name, execute/executescript/commit/close -
and this module presents it over SQLite (the default) or a MySQL-compatible
server when configured. No other module knows which engine is behind the
connection.

Two variables, engine and connection, set independently:

  FIRESIDE_DB_TYPE=sqlite|mysql      the engine (default: sqlite)
  FIRESIDE_DB_CONN=<connection>      sqlite: a file path, or sqlite:///path ('' =
                                     the default under FIRESIDE_DATA)
                                     mysql:  user:pass@host:3306/dbname, or a
                                     full mysql://... DSN

The legacy single-variable form still works, so an existing deployment need not
move: FIRESIDE_DB=mysql://user:pass@host:3306/fireside (or sqlite:///path/to.db)
sets both the engine and the connection when the two variables above are unset.

The schema uses only types both engines accept: VARCHAR keys (MySQL needs a
key length; SQLite does not care), TEXT for prose, DOUBLE and BIGINT for numbers.
Reserved-word columns are quoted with backticks, which both engines understand.
"""

import os
import re
import sqlite3
import threading
import time
import urllib.parse

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id VARCHAR(128) PRIMARY KEY, name VARCHAR(255) DEFAULT '', created_at VARCHAR(32));
CREATE TABLE IF NOT EXISTS books (
    id VARCHAR(128) PRIMARY KEY, user_id VARCHAR(128), title TEXT,
    source_json TEXT, cover_ref VARCHAR(255) DEFAULT '',
    cover_mime VARCHAR(64) DEFAULT '', author VARCHAR(255) DEFAULT '',
    isbn VARCHAR(32) DEFAULT '', publisher VARCHAR(255) DEFAULT '',
    published VARCHAR(64) DEFAULT '', description TEXT,
    created_at VARCHAR(32));
CREATE TABLE IF NOT EXISTS chapters (
    book_id VARCHAR(128), idx INT, title TEXT, chars INT,
    text_ref VARCHAR(255), audio_ref VARCHAR(255), audio_bytes INT, duration_s DOUBLE,
    provider VARCHAR(64), voice VARCHAR(128), model VARCHAR(128), speed DOUBLE,
    PRIMARY KEY (book_id, idx));
CREATE TABLE IF NOT EXISTS series (
    id VARCHAR(128) PRIMARY KEY, user_id VARCHAR(128), name VARCHAR(255),
    created_at VARCHAR(32));
CREATE TABLE IF NOT EXISTS series_books (
    series_id VARCHAR(128), book_id VARCHAR(128), place INT,
    PRIMARY KEY (series_id, book_id));
CREATE TABLE IF NOT EXISTS pron_dicts (
    id VARCHAR(128) PRIMARY KEY, user_id VARCHAR(128), name VARCHAR(255),
    scope VARCHAR(16) DEFAULT 'global', scope_key VARCHAR(255) DEFAULT '',
    rules TEXT, created_at VARCHAR(32), updated_at VARCHAR(32));
CREATE TABLE IF NOT EXISTS render_cache (
    `key` VARCHAR(191) PRIMARY KEY, audio_ref VARCHAR(255), audio_bytes INT,
    duration_s DOUBLE, provider VARCHAR(64), voice VARCHAR(128),
    model VARCHAR(128), created_at VARCHAR(32));
CREATE TABLE IF NOT EXISTS renditions (
    rk VARCHAR(191) PRIMARY KEY, book_id VARCHAR(128), idx INT,
    provider VARCHAR(64), voice VARCHAR(128), voice_label VARCHAR(255),
    model VARCHAR(128), speed DOUBLE, pron VARCHAR(16) DEFAULT '',
    audio_ref VARCHAR(255), audio_bytes INT,
    duration_s DOUBLE, created_at VARCHAR(32));
CREATE TABLE IF NOT EXISTS jobs (
    id VARCHAR(128) PRIMARY KEY, user_id VARCHAR(128), book_id VARCHAR(128), kind VARCHAR(32),
    provider VARCHAR(64), voice VARCHAR(128), model VARCHAR(128), status VARCHAR(32),
    total INT, current INT, error TEXT,
    created_at VARCHAR(32), updated_at VARCHAR(32));
CREATE TABLE IF NOT EXISTS ledger (
    id VARCHAR(128) PRIMARY KEY, user_id VARCHAR(128), kind VARCHAR(32), delta DOUBLE,
    currency VARCHAR(16), book_id VARCHAR(128), job_id VARCHAR(128), note TEXT,
    created_at VARCHAR(32));
CREATE TABLE IF NOT EXISTS balances (
    user_id VARCHAR(128) PRIMARY KEY, currency VARCHAR(16), amount DOUBLE);
CREATE TABLE IF NOT EXISTS reservations (
    id VARCHAR(128) PRIMARY KEY, user_id VARCHAR(128), amount DOUBLE, currency VARCHAR(16),
    state VARCHAR(32), job_id VARCHAR(128), created_at VARCHAR(32), updated_at VARCHAR(32));
CREATE TABLE IF NOT EXISTS positions (
    user_id VARCHAR(128), book_id VARCHAR(128), chapter_index INT, offset_seconds DOUBLE,
    updated_at VARCHAR(32), owner_device_id VARCHAR(128),
    PRIMARY KEY (user_id, book_id));
CREATE TABLE IF NOT EXISTS devices (
    user_id VARCHAR(128), device_id VARCHAR(128), book_id VARCHAR(128), last_seen VARCHAR(32),
    stop INT DEFAULT 0, PRIMARY KEY (user_id, device_id, book_id));
CREATE TABLE IF NOT EXISTS listens (
    user_id VARCHAR(128), book_id VARCHAR(128), seconds DOUBLE, created_at VARCHAR(32));
CREATE TABLE IF NOT EXISTS credentials (
    user_id VARCHAR(128), provider VARCHAR(64), data TEXT, updated_at VARCHAR(32),
    PRIMARY KEY (user_id, provider));
CREATE TABLE IF NOT EXISTS sessions (
    token VARCHAR(191) PRIMARY KEY, user_id VARCHAR(128), provider VARCHAR(32),
    created_at BIGINT, expires_at BIGINT);
CREATE TABLE IF NOT EXISTS local_accounts (
    username VARCHAR(191) PRIMARY KEY, user_id VARCHAR(128), digest TEXT, salt TEXT,
    email VARCHAR(255) DEFAULT '', created_at VARCHAR(32));
CREATE TABLE IF NOT EXISTS identities (
    provider VARCHAR(64), subject VARCHAR(191), user_id VARCHAR(128),
    email VARCHAR(255) DEFAULT '', email_verified INT DEFAULT 0,
    created_at VARCHAR(32), PRIMARY KEY (provider, subject));
CREATE TABLE IF NOT EXISTS password_resets (
    token_hash VARCHAR(191) PRIMARY KEY, user_id VARCHAR(128),
    created_at BIGINT, expires_at BIGINT, used_at BIGINT DEFAULT 0);
CREATE TABLE IF NOT EXISTS login_claims (
    token VARCHAR(191) PRIMARY KEY, provider VARCHAR(64), subject VARCHAR(191),
    name VARCHAR(255) DEFAULT '', email VARCHAR(255) DEFAULT '',
    email_verified INT DEFAULT 0, user_id VARCHAR(128),
    created_at BIGINT, expires_at BIGINT);
CREATE TABLE IF NOT EXISTS app_handoffs (
    token_hash VARCHAR(191) PRIMARY KEY, user_id VARCHAR(128),
    created_at BIGINT, expires_at BIGINT, used_at BIGINT DEFAULT 0);
CREATE TABLE IF NOT EXISTS admins (
    user_id VARCHAR(128) PRIMARY KEY, created_at VARCHAR(32));
CREATE TABLE IF NOT EXISTS settings (
    `key` VARCHAR(191) PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS connections (
    id VARCHAR(128) PRIMARY KEY, owner_id VARCHAR(128), scope VARCHAR(16),
    provider VARCHAR(64), label VARCHAR(255), data TEXT,
    created_at VARCHAR(32), updated_at VARCHAR(32));
CREATE TABLE IF NOT EXISTS connection_overrides (
    user_id VARCHAR(128), connection_id VARCHAR(128), enabled INT DEFAULT 1,
    data TEXT, updated_at VARCHAR(32),
    PRIMARY KEY (user_id, connection_id));
CREATE TABLE IF NOT EXISTS user_prefs (
    user_id VARCHAR(128) PRIMARY KEY, default_connection_id VARCHAR(128),
    default_voice VARCHAR(255), pron TEXT, reading TEXT, updated_at VARCHAR(32));
CREATE TABLE IF NOT EXISTS subscriptions (
    user_id VARCHAR(128) PRIMARY KEY, plan VARCHAR(64), status VARCHAR(32),
    provider VARCHAR(32), reference VARCHAR(191), current_period_end BIGINT DEFAULT 0,
    updated_at VARCHAR(32), created_at VARCHAR(32));
CREATE TABLE IF NOT EXISTS addons (
    user_id VARCHAR(128), addon VARCHAR(64), quantity INT DEFAULT 0,
    status VARCHAR(32), provider VARCHAR(32), reference VARCHAR(191),
    current_period_end BIGINT DEFAULT 0, updated_at VARCHAR(32), created_at VARCHAR(32),
    PRIMARY KEY (user_id, addon));
CREATE TABLE IF NOT EXISTS payment_events (
    id VARCHAR(191) PRIMARY KEY, kind VARCHAR(32), user_id VARCHAR(128),
    reference VARCHAR(191), chars INT DEFAULT 0, detail TEXT,
    created_at VARCHAR(32));
CREATE TABLE IF NOT EXISTS idempotency (
    user_id VARCHAR(128), kkey VARCHAR(191), status INT DEFAULT 0,
    response TEXT, created_at VARCHAR(32), PRIMARY KEY (user_id, kkey));
CREATE TABLE IF NOT EXISTS user_grants (
    user_id VARCHAR(128) PRIMARY KEY,
    storage_unlimited INT DEFAULT 0, credits_unlimited INT DEFAULT 0,
    updated_at VARCHAR(32));
"""


def default_data_dir():
    return os.environ.get(
        "FIRESIDE_DATA",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"),
    )


def _env(name):
    return (os.environ.get(name) or "").strip()


def db_type(explicit=None):
    """The configured engine: 'sqlite' (default) or 'mysql'.

    FIRESIDE_DB_TYPE names it directly. When that is unset the engine is read
    from the legacy FIRESIDE_DB URL, so a single-variable deployment is unchanged.
    """
    if explicit:
        return "mysql" if str(explicit).strip().lower() in ("mysql", "mariadb") else "sqlite"
    t = _env("FIRESIDE_DB_TYPE").lower()
    if t in ("mysql", "mariadb"):
        return "mysql"
    if t in ("sqlite", "sqlite3"):
        return "sqlite"
    return "mysql" if _env("FIRESIDE_DB").startswith("mysql") else "sqlite"


def db_conn(explicit=None):
    """The connection string for the configured engine.

    Unset (or empty) means the SQLite default under FIRESIDE_DATA. Falls back to
    the legacy FIRESIDE_DB when FIRESIDE_DB_CONN is unset.
    """
    if explicit is not None:
        return explicit
    return _env("FIRESIDE_DB_CONN") or _env("FIRESIDE_DB")


def db_url():
    """One connection string for the configured backend, engine included."""
    kind = db_type()
    conn = db_conn()
    if kind == "mysql":
        if not conn:
            return ""
        return conn if conn.startswith("mysql") else "mysql://" + conn
    if not conn or conn.startswith("sqlite://"):
        return conn
    return "sqlite://" + conn


def mysql_ssl_kwargs():
    """Extra pymysql.connect kwargs that put a MySQL connection on TLS.

    Empty unless FIRESIDE_DB_SSL is set, so a LAN self-host that speaks to a
    plaintext MySQL is unchanged. A server with `require_secure_transport=ON`
    (Azure Database for MySQL) refuses a plaintext connection outright, and
    pymysql only negotiates TLS when handed an SSL context - so a hosted
    deployment opts in here.

    Values: `1`/`verify`/`require` verify the server certificate against the
    system trust store; `noverify`/`insecure`/`encrypt` still encrypt the
    channel but skip verification.
    """
    mode = _env("FIRESIDE_DB_SSL").lower()
    if not mode:
        return {}
    import ssl
    ctx = ssl.create_default_context()
    if mode in ("noverify", "insecure", "encrypt"):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return {"ssl": ctx}


def backend_name(url=None):
    if url is None:
        return db_type()
    return "mysql" if url.startswith("mysql") else "sqlite"


def sqlite_path(path=None):
    if path is None:
        d = default_data_dir()
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "fireside.db")
    elif path != ":memory:":
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
    return path


def _columns(conn, table):
    """The column names of `table` on whichever engine is behind `conn`."""
    if db_type() == "mysql":
        rows = conn.execute("SHOW COLUMNS FROM `%s`" % table).fetchall()
        return {(r["Field"] if isinstance(r, dict) else r[0]) for r in rows}
    rows = conn.execute("PRAGMA table_info(%s)" % table).fetchall()
    return {r["name"] for r in rows}


# Columns added after the first schema shipped. An existing database predates
# them, and CREATE TABLE IF NOT EXISTS will not touch a table that is already
# there, so each one is added in place. Idempotent and safe on both engines.
_ADDED_COLUMNS = [
    ("jobs", "done_chars", "INT DEFAULT 0"),
    ("books", "cover_ref", "VARCHAR(255) DEFAULT ''"),
    ("books", "cover_mime", "VARCHAR(64) DEFAULT ''"),
    ("books", "author", "VARCHAR(255) DEFAULT ''"),
    ("books", "isbn", "VARCHAR(32) DEFAULT ''"),
    ("books", "publisher", "VARCHAR(255) DEFAULT ''"),
    ("books", "published", "VARCHAR(64) DEFAULT ''"),
    ("books", "description", "TEXT"),
    # a reader changed the looked-up metadata by hand, so a later lookup must
    # never quietly decide it knows better.
    ("books", "user_edited", "INT DEFAULT 0"),
    ("user_prefs", "pron", "TEXT"),
    # how the player reads along: highlight, scroll, scroll mode. JSON, because
    # it is one small bag of preferences and not a thing anything else queries.
    ("user_prefs", "reading", "TEXT"),
    ("renditions", "pron", "VARCHAR(16) DEFAULT ''"),
    # device tracking on a session: a public id to name it, and what it is.
    ("sessions", "sid", "VARCHAR(32) DEFAULT ''"),
    ("sessions", "user_agent", "TEXT"),
    ("sessions", "ip", "VARCHAR(64) DEFAULT ''"),
    ("sessions", "last_seen", "BIGINT DEFAULT 0"),
    # the address a password account registered with. Optional on old rows;
    # the social door reads it to spot an account that already exists.
    ("local_accounts", "email", "VARCHAR(255) DEFAULT ''"),
    # the Stripe objects behind a recurring row. The subscription id is what a
    # cancel or a lifecycle event names; without it the host can only guess.
    ("subscriptions", "customer", "VARCHAR(64) DEFAULT ''"),
    ("subscriptions", "subscription_id", "VARCHAR(64) DEFAULT ''"),
    ("addons", "customer", "VARCHAR(64) DEFAULT ''"),
    ("addons", "subscription_id", "VARCHAR(64) DEFAULT ''"),
    # the series a book's own file claims (EPUB calibre:series) and where in it,
    # separate from the shelves a reader builds. A pronunciation dict can attach
    # to either name; this one arrives from the file, not from the reader.
    ("books", "series_name", "VARCHAR(255) DEFAULT ''"),
    ("books", "series_index", "VARCHAR(32) DEFAULT ''"),
]


def migrate(conn):
    """Bring an existing database up to the current schema, in place."""
    for table, name, ddl in _ADDED_COLUMNS:
        if name in _columns(conn, table):
            continue
        try:
            conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, ddl))
            conn.commit()
        except Exception:               # a concurrent migration already did it
            pass
    return conn


def connect(path=None):
    """Open the configured backend. `path` is the SQLite file (default dir)."""
    if db_type() == "mysql":
        conn = MySQLConnection(db_url())
        conn.executescript(SCHEMA)
        return migrate(conn)
    url = db_conn()
    if path is None and url:
        if url.startswith("sqlite://"):
            path = url[len("sqlite://"):] or None
        else:
            path = url
    conn = SQLiteConnection(sqlite_path(path))
    conn.executescript(SCHEMA)
    conn.commit()
    return migrate(conn)


class Result:
    """The rows one statement produced, already in memory.

    sqlite3 hands back a live cursor that owns its statement, and a cursor is
    only safe while nobody else touches the connection. On a host that serves
    requests from a thread pool over one shared connection, another thread's
    execute() between our execute() and our fetchall() is exactly how a reader
    gets an InterfaceError ("bad parameter or other API misuse") or an empty
    answer for a row that is sitting right there. Reading the rows out while
    the lock is still held makes the statement and its results one indivisible
    step, so the caller's cursor cannot be taken out from under it.

    Shaped like the subset of DB-API the host actually uses: fetchone,
    fetchall, iteration, rowcount, lastrowid.
    """

    def __init__(self, rows, rowcount, lastrowid):
        self._rows = rows
        self._at = 0
        self.rowcount = rowcount
        self.lastrowid = lastrowid

    def fetchone(self):
        if self._at >= len(self._rows):
            return None
        row = self._rows[self._at]
        self._at += 1
        return row

    def fetchall(self):
        rest = self._rows[self._at:]
        self._at = len(self._rows)
        return rest

    def __iter__(self):
        rest = self.fetchall()
        return iter(rest)

    def __len__(self):
        return len(self._rows)


class SQLiteConnection:
    """One SQLite connection, safe to share across request threads.

    The host is a ThreadingHTTPServer and every thread reaches for the same
    App.conn, so statements from different threads land on one sqlite3
    connection at the same time. sqlite3 releases the GIL around a statement,
    which makes that interleaving real, and CPython's connection object is not
    built for it: the symptom is intermittent, not loud. A SELECT for a session
    that exists comes back empty (the page decides the reader is logged out and
    puts the sign-in door back over their books) or raises InterfaceError
    ("bad parameter or other API misuse", which the session lookup turns into a
    503). Neither reproduces on one core, which is why it looked like a CI
    ghost: it needs two statements in flight at once.

    So every statement runs under one lock and its rows are read out before the
    lock is released. That is the same decision MySQLConnection makes above,
    for the same reason: for a household server, one statement at a time is
    exactly the right amount of concurrency control, and correctness at this
    layer is worth more than parallel reads on a book library.

    Transactions stay connection-wide (commit commits, rollback rolls back),
    which is what every module already assumes. Callers that need to undo only
    their own work should use a SAVEPOINT rather than a bare ROLLBACK: a
    connection-wide rollback also discards whatever another thread had pending.
    """

    def __init__(self, path):
        self._lock = threading.RLock()
        self._raw = sqlite3.connect(path, check_same_thread=False)
        self._raw.row_factory = sqlite3.Row

    def execute(self, sql, params=()):
        with self._lock:
            cur = self._raw.execute(sql, tuple(params) if params else ())
            rows = cur.fetchall()
            return Result(rows, cur.rowcount, cur.lastrowid)

    def executescript(self, script):
        with self._lock:
            self._raw.executescript(script)

    def commit(self):
        with self._lock:
            self._raw.commit()

    def rollback(self):
        with self._lock:
            self._raw.rollback()

    @property
    def in_transaction(self):
        with self._lock:
            return self._raw.in_transaction

    def close(self):
        with self._lock:
            self._raw.close()


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---- MySQL shim -------------------------------------------------------------
# SQLite-isms in the query layer, translated to what MySQL accepts. The list is
# deliberately short and explicit: everything else the host writes is plain
# SQL-92 and runs unchanged on both engines.

_INSERT_OR_IGNORE = re.compile(r"\bINSERT\s+OR\s+IGNORE\b", re.I)
_INSERT_OR_REPLACE = re.compile(r"\bINSERT\s+OR\s+REPLACE\b", re.I)
# SQLite's upsert clause: ON CONFLICT(cols) DO UPDATE SET a=excluded.a, ...
# MySQL has no ON CONFLICT; the equivalent is ON DUPLICATE KEY UPDATE a=VALUES(a).
_ON_CONFLICT = re.compile(
    r"\bON\s+CONFLICT\s*\(([^)]*)\)\s+DO\s+UPDATE\s+SET\s+(.+)$",
    re.I | re.S)


def _on_duplicate_key(m):
    """Rewrite one ON CONFLICT body to MySQL's ON DUPLICATE KEY UPDATE.

    Each `col=excluded.col` becomes `col=VALUES(col)`. A right-hand side that
    is not `excluded.<col>` is left untouched, so an expression the two engines
    share still passes through.
    """
    parts = []
    for clause in m.group(2).split(","):
        col, sep, val = clause.partition("=")
        val = val.strip()
        if sep and val.lower().startswith("excluded."):
            parts.append("%s=VALUES(%s)" % (col.strip(), val[len("excluded."):]))
        else:
            parts.append(clause.strip())
    return "ON DUPLICATE KEY UPDATE " + ",".join(parts)


def translate(sql):
    """Rewrite a SQLite-flavoured statement for MySQL. Pure, so it is testable."""
    sql = _INSERT_OR_IGNORE.sub("INSERT IGNORE", sql)
    sql = _INSERT_OR_REPLACE.sub("REPLACE", sql)
    if sql.strip().upper().startswith("BEGIN IMMEDIATE"):
        sql = "START TRANSACTION"
    sql = _ON_CONFLICT.sub(_on_duplicate_key, sql)
    return sql.replace("?", "%s")


class MySQLConnection:
    """A DB-API connection shaped like the sqlite one the host already speaks.

    All access is serialised behind one lock: a pymysql connection is not
    thread-safe and the host serves requests from a thread pool. For a home
    server that is exactly the right amount of concurrency control.
    """

    def __init__(self, url, cursor_dict=True):
        import pymysql  # imported here so SQLite deployments need not install it

        u = urllib.parse.urlparse(url.replace("mysql+pymysql", "mysql", 1))
        self._pymysql = pymysql
        self._lock = threading.RLock()
        self._in_txn = False
        dbname = (u.path or "").lstrip("/") or "fireside"
        safe = re.sub(r"[^A-Za-z0-9_]", "", dbname) or "fireside"
        base = dict(
            host=u.hostname or "127.0.0.1",
            port=u.port or 3306,
            user=urllib.parse.unquote(u.username or ""),
            password=urllib.parse.unquote(u.password or ""),
            charset="utf8mb4",
            autocommit=True,
            cursorclass=pymysql.cursors.DictCursor,
            **mysql_ssl_kwargs(),
        )
        boot = pymysql.connect(**base)          # the database may not exist yet
        try:
            with boot.cursor() as c:
                c.execute("CREATE DATABASE IF NOT EXISTS `%s` "
                          "CHARACTER SET utf8mb4" % safe)
        finally:
            boot.close()
        self._raw = pymysql.connect(database=safe, **base)

    def _ping(self):
        try:
            self._raw.ping(reconnect=True)
        except Exception:
            pass

    def execute(self, sql, params=()):
        with self._lock:
            self._ping()
            cur = self._raw.cursor()
            cur.execute(translate(sql), tuple(params) if params else ())
            head = sql.strip().upper()
            if head.startswith("START TRANSACTION") or head.startswith("BEGIN IMMEDIATE"):
                self._in_txn = True
            elif head.startswith("COMMIT") or head.startswith("ROLLBACK"):
                self._in_txn = False
            return cur

    def executescript(self, script):
        with self._lock:
            cur = self._raw.cursor()
            for stmt in script.split(";"):
                s = stmt.strip()
                if s:
                    cur.execute(s)
            self._raw.commit()
            self._in_txn = False

    def commit(self):
        with self._lock:
            self._raw.commit()
            self._in_txn = False

    def rollback(self):
        with self._lock:
            self._raw.rollback()
            self._in_txn = False

    @property
    def in_transaction(self):
        return self._in_txn

    def close(self):
        with self._lock:
            self._raw.close()

