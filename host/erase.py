"""Erasing an account: the user's rows gone, and their bytes off the disk.

A deletion is not the same act as emptying a shelf. Removing a book frees the
shelf and lets a shared blob stay; erasing an account has to leave nothing of
this person behind that no one else still needs. Two rules hold it honest:

- a blob another account still names stays (the shared example book, a public
  text two people imported), because deleting it would break a surviving reader;
- a blob only this account named goes, INCLUDING the copy the shared render
  cache was holding for it. The cache exists so a re-voice is free, not so a
  deleted reader's audio survives a deletion request.
"""

from host import library, playback, stats
from host.db import now  # noqa: F401  (kept for callers that stamp a trace)

# every ref a book owns: its chapter text and audio, its renditions, its cover.
_BOOK_REF_SQL = (
    ("SELECT text_ref AS ref FROM chapters WHERE book_id=?",),
    ("SELECT audio_ref AS ref FROM chapters WHERE book_id=?",),
    ("SELECT audio_ref AS ref FROM renditions WHERE book_id=?",),
    ("SELECT cover_ref AS ref FROM books WHERE id=?",),
)

# the rows that mean "a live reader still needs this blob". The render cache is
# deliberately absent: a cache entry alone is not a live reader.
_LIVE_REF_SQL = (
    ("SELECT 1 FROM chapters WHERE text_ref=?",),
    ("SELECT 1 FROM chapters WHERE audio_ref=?",),
    ("SELECT 1 FROM renditions WHERE audio_ref=?",),
    ("SELECT 1 FROM books WHERE cover_ref=?",),
)

# rows keyed by the account itself. Everything a person leaves behind that is
# theirs alone: work, money, devices, history, sign-ins, and settings.
_USER_TABLES = (
    "jobs", "ledger", "balances", "reservations", "positions", "devices",
    "listens", "credentials", "sessions", "local_accounts", "identities",
    "password_resets", "login_claims", "app_handoffs", "user_prefs", "subscriptions", "addons",
    "user_grants", "connection_overrides", "idempotency", "payment_events",
)


def book_refs(conn, book_id):
    """Every blob ref a book names, text/audio/cover."""
    refs = []
    for (sql,) in _BOOK_REF_SQL:
        for row in conn.execute(sql, (book_id,)).fetchall():
            if row["ref"]:
                refs.append(row["ref"])
    return refs


def _live_reader(conn, ref):
    """True when a surviving row (not the render cache) still names this blob."""
    for (sql,) in _LIVE_REF_SQL:
        if conn.execute(sql, (ref,)).fetchone():
            return True
    return False


def purge_user(conn, storage, user_id, books=None):
    """Erase one account. Returns {books, freed}.

    `books` lets a caller that already listed the shelf hand it in; otherwise
    the shelf is read here. The order matters: gather the refs, drop the book
    rows, then for each ref that no live reader still names, drop the render
    cache entry and the blob. A shared blob survives because _live_reader true.
    """
    if books is None:
        books = library.list_books(conn, user_id)
    refs = []
    for b in books:
        bid = b["id"]
        refs.extend(book_refs(conn, bid))
        playback.discard_book(conn, user_id, bid)
        stats.discard_book(conn, user_id, bid)
        library.delete_book(conn, storage, bid)  # rows gone; shared blobs stay
    freed = 0
    for ref in {r for r in refs if r}:
        if _live_reader(conn, ref):
            continue
        conn.execute("DELETE FROM render_cache WHERE audio_ref=?", (ref,))
        try:
            storage.delete(ref)
            freed += 1
        except Exception:
            pass
    for table in _USER_TABLES:
        conn.execute("DELETE FROM %s WHERE user_id=?" % table, (user_id,))
    # a user's own connections (never a system one: the sale connection and any
    # admin-shared instance belong to the server, not to the account)
    conn.execute("DELETE FROM connections WHERE owner_id=? AND scope<>'system'",
                 (user_id,))
    conn.execute("DELETE FROM admins WHERE user_id=?", (user_id,))
    conn.execute("DELETE FROM users WHERE id=?", (user_id,))
    conn.commit()
    return {"books": len(books), "freed": freed}
