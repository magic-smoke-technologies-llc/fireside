"""The library: books and their chapters, per user, with text behind refs.

The extracted chapter TEXT is stored content-addressed, so the same chapter text
(from an epub or the same fic fetched as html) resolves to one blob and one
render cache entry. A book row names its source in `meta`; a chapter row names
its text blob and, once rendered, its audio blob.
"""

import hashlib
import json
import uuid

from host.db import now


def create_user(conn, user_id, name=""):
    conn.execute("INSERT OR IGNORE INTO users(id,name,created_at) VALUES(?,?,?)",
                 (user_id, name, now()))
    conn.commit()
    return user_id


def add_book(conn, storage, user_id, book):
    """Store book + chapters. `book` is a host.contracts.Book. Returns the id."""
    book_id = book.id or uuid.uuid4().hex
    meta = dict(book.meta or {})
    conn.execute(
        # The author is metadata the reader already extracted. Writing it to
        # the column is what makes the shelf able to show it without parsing
        # source_json per row.
        "INSERT INTO books(id,user_id,title,source_json,author,created_at)"
        " VALUES(?,?,?,?,?,?)",
        (book_id, user_id, book.title, json.dumps(meta), meta.get("author", ""),
         now()))
    for i, ch in enumerate(book.chapters):
        text = ch.text if isinstance(ch.text, str) else str(ch.text)
        ref = storage.put(text.encode("utf-8"))
        conn.execute(
            "INSERT OR REPLACE INTO chapters"
            "(book_id,idx,title,chars,text_ref) VALUES(?,?,?,?,?)",
            (book_id, i, ch.title, len(text), ref))
    conn.commit()
    return book_id


def find_same(conn, user_id, incoming, plugin=None):
    """The id of this user's book that holds the same work as `incoming` meta.

    Two ways to be the same book: the reader named a stable identity on both
    sides, or the reader's own `same()` says so. The second covers books that
    were imported before readers named an identity at all (a web page fetched by
    the generic reader, its URL since claimed by a site reader), which is why it
    is asked even when the stored row has no identity.
    """
    want = (incoming or {}).get("identity") or ""
    same = getattr(plugin, "same", None) if plugin is not None else None
    for r in conn.execute("SELECT id,source_json FROM books WHERE user_id=?",
                          (user_id,)).fetchall():
        try:
            meta = json.loads(r["source_json"] or "{}")
        except ValueError:
            meta = {}
        if not isinstance(meta, dict):
            continue
        if want and meta.get("identity") == want:
            return r["id"]
        if callable(same):
            try:
                if same(meta, incoming or {}):
                    return r["id"]
            except Exception:
                continue
    return None


def shelf_bytes(conn, book_id):
    """Upper-bound bytes this shelf row already accounts for: its chapter text,
    its voiced audio, and its cover. Used to ask whether a replacement needs
    room, without refusing a swap that is roughly the same size."""
    total = 0
    for c in chapters(conn, book_id):
        total += c["chars"] or 0
        total += c["audio_bytes"] or 0
    for r in renditions(conn, book_id):
        total += r["audio_bytes"] or 0
    b = conn.execute("SELECT cover_ref FROM books WHERE id=?", (book_id,)).fetchone()
    if b and b["cover_ref"]:
        total += 4096  # a cover is a small image; this only needs the order of magnitude
    return total


def replace_book(conn, storage, book_id, book):
    """Re-import over a book already on the shelf, in place.

    The id stays, so playback positions, device stops and listen stats still
    point at this book, and a voice whose chapter text is byte-identical is
    kept: chapter text is content-addressed, so the same ref means the audio
    still matches the words and there is nothing to re-synthesise. A chapter
    whose text changed loses its voice, because that audio belongs to the old
    text. A chapter the reader no longer returns is removed with its voices.
    """
    meta = dict(book.meta or {})
    old = {c["idx"]: c for c in chapters(conn, book_id)}
    refs, kept, dropped = [], 0, 0
    new_idx = set()
    for i, ch in enumerate(book.chapters):
        new_idx.add(i)
        text = ch.text if isinstance(ch.text, str) else str(ch.text)
        ref = storage.put(text.encode("utf-8"))
        o = old.get(i)
        if o is not None and o["text_ref"] == ref and o["audio_ref"]:
            conn.execute(
                "INSERT OR REPLACE INTO chapters(book_id,idx,title,chars,text_ref,"
                "audio_ref,audio_bytes,duration_s,provider,voice,model,speed)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (book_id, i, ch.title, len(text), ref, o["audio_ref"],
                 o["audio_bytes"], o["duration_s"], o["provider"], o["voice"],
                 o["model"], o["speed"]))
            kept += 1
            continue
        conn.execute(
            "INSERT OR REPLACE INTO chapters(book_id,idx,title,chars,text_ref)"
            " VALUES(?,?,?,?,?)", (book_id, i, ch.title, len(text), ref))
        if o is not None:
            # The row is replaced wholesale above, so the text blob it named is
            # about to be unnamed; gc keeps it if any other shelf still reads it.
            refs.append(o["text_ref"])
            if o["audio_ref"]:
                refs.append(o["audio_ref"])
        gone = [r for r in renditions(conn, book_id) if r["idx"] == i]
        for r in gone:
            conn.execute("DELETE FROM renditions WHERE rk=?", (r["rk"],))
            if r["audio_ref"]:
                refs.append(r["audio_ref"])
            dropped += 1
    for i, o in old.items():
        if i in new_idx:
            continue
        conn.execute("DELETE FROM chapters WHERE book_id=? AND idx=?", (book_id, i))
        if o["text_ref"]:
            refs.append(o["text_ref"])
        if o["audio_ref"]:
            refs.append(o["audio_ref"])
        for r in [r for r in renditions(conn, book_id) if r["idx"] == i]:
            conn.execute("DELETE FROM renditions WHERE rk=?", (r["rk"],))
            if r["audio_ref"]:
                refs.append(r["audio_ref"])
            dropped += 1
    # Title: the shelf already belongs to this account and may have been
    # renamed from the book page, so an empty title is the only one the import
    # fills. The rest of the description is the reader's to bring.
    row = conn.execute("SELECT title,author FROM books WHERE id=?", (book_id,)).fetchone()
    title = (row["title"] if row and row["title"] else book.title) or "Untitled"
    author = (row["author"] if "author" in row.keys() and row["author"]
              else meta.get("author", ""))
    conn.execute("UPDATE books SET title=?,source_json=?,author=? WHERE id=?",
                 (title, json.dumps(meta), author, book_id))
    conn.commit()
    freed = gc_blobs(conn, storage, refs)
    return {"kept_renditions": kept, "dropped_renditions": dropped, "freed": freed}


def find_by_meta(conn, user_id, key, value):
    """The id of this user's book whose source meta[key] == value, or None.

    Used to find a copy of a bundled book already on the shelf (the example)
    without matching on its title, which a reader may have edited."""
    rows = conn.execute("SELECT id,source_json FROM books WHERE user_id=?",
                        (user_id,)).fetchall()
    for r in rows:
        try:
            meta = json.loads(r["source_json"] or "{}")
        except ValueError:
            meta = {}
        if isinstance(meta, dict) and meta.get(key) == value:
            return r["id"]
    return None


def list_books(conn, user_id):
    rows = conn.execute(
        "SELECT * FROM books WHERE user_id=? ORDER BY created_at DESC", (user_id,))
    # The shelf is the one place a series has to be visible as a container, so
    # memberships ride along with the books in two queries instead of two per
    # book. Places are stored 0-based and reported 1-based, same as the panel.
    mine = {}
    for m in conn.execute(
            "SELECT b.book_id, s.id, s.name, b.place FROM series_books b"
            " JOIN series s ON s.id=b.series_id WHERE s.user_id=?", (user_id,)):
        mine.setdefault(m["book_id"], []).append(
            {"id": m["id"], "name": m["name"], "place": (m["place"] or 0) + 1})
    counts = {r["id"]: r["c"] for r in conn.execute(
        "SELECT series_id id, COUNT(*) c FROM series_books GROUP BY series_id")}
    out = []
    for r in rows:
        n = conn.execute("SELECT COUNT(*) c FROM chapters WHERE book_id=?",
                         (r["id"],)).fetchone()["c"]
        done = conn.execute(
            "SELECT COUNT(*) c FROM chapters WHERE book_id=? AND audio_ref IS NOT NULL",
            (r["id"],)).fetchone()["c"]
        out.append({"id": r["id"], "title": r["title"],
                    "author": (r["author"] or ""),
                    "created_at": r["created_at"], "chapters": n, "rendered": done,
                    "cover": r["cover_ref"] or "",
                    "series": [dict(s, count=counts.get(s["id"], 1))
                               for s in mine.get(r["id"], [])]})
    return out


def get_book(conn, book_id):
    r = conn.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
    if not r:
        return None
    meta = json.loads(r["source_json"] or "{}")
    return {"id": r["id"], "user_id": r["user_id"], "title": r["title"],
            "meta": meta, "created_at": r["created_at"],
            "cover": r["cover_ref"] or "", "cover_mime": r["cover_mime"] or "",
            "author": r["author"] or "", "publisher": r["publisher"] or "",
            "published": r["published"] or "", "description": r["description"] or "",
            "user_edited": 1 if r["user_edited"] else 0,
            # the ISBN column is where it lives now; an old row may still carry
            # one in its source metadata, so fall back to that.
            "isbn": (r["isbn"] or "") or (meta or {}).get("isbn", "")}


# The descriptive columns a book can carry, edited from the book-settings panel.
_DETAIL_FIELDS = ("title", "author", "isbn", "publisher", "published", "description")


def set_details(conn, book_id, **fields):
    """Update the descriptive fields on a book. Only the known columns are
    touched (the names come from a fixed list, never from the caller)."""
    cols = [k for k in _DETAIL_FIELDS if k in fields]
    if not cols:
        return
    sets = ",".join("%s=?" % c for c in cols)
    conn.execute("UPDATE books SET %s WHERE id=?" % sets,
                 tuple(fields[c] for c in cols) + (book_id,))
    conn.commit()


def set_user_details(conn, book_id, **fields):
    """The reader edited the metadata by hand.

    Same columns, but it also raises `user_edited`, which is the difference
    between "the reader typed this" and "a lookup filled this in". Once it is
    raised, a fresh ISBN lookup is a suggestion the reader has to accept, not
    something that lands on top of their fix.
    """
    cols = [k for k in _DETAIL_FIELDS if k in fields]
    sets = ["%s=?" % c for c in cols] + ["user_edited=1"]
    conn.execute("UPDATE books SET %s WHERE id=?" % ",".join(sets),
                 tuple(fields[c] for c in cols) + (book_id,))
    conn.commit()


# ---- series ---------------------------------------------------------------
# A series is a named container a reader creates and hangs their own books on,
# in an order. It is local to the account; nothing here is published. Membership
# is its own row carrying a `place`, so a book's position is a number you can
# move and the same book in a second series later is one more row rather than a
# schema change. The order only ever affects display: nothing about audio,
# identity or replacement reads it.

SERIES_MAX_NAME = 120


def _own_series(conn, user_id, series_id):
    return conn.execute("SELECT id,name FROM series WHERE id=? AND user_id=?",
                        (series_id, user_id)).fetchone()


def create_series(conn, user_id, name):
    name = " ".join(str(name or "").split())[:SERIES_MAX_NAME]
    if not name:
        return None
    sid = uuid.uuid4().hex
    conn.execute("INSERT INTO series(id,user_id,name,created_at) VALUES(?,?,?,?)",
                 (sid, user_id, name, now()))
    conn.commit()
    return sid


def list_series(conn, user_id):
    rows = conn.execute(
        "SELECT id,name,created_at FROM series WHERE user_id=? ORDER BY name,created_at",
        (user_id,)).fetchall()
    out = []
    for r in rows:
        n = conn.execute("SELECT COUNT(*) c FROM series_books WHERE series_id=?",
                         (r["id"],)).fetchone()["c"]
        out.append({"id": r["id"], "name": r["name"], "books": n,
                    "created_at": r["created_at"]})
    return out


def rename_series(conn, user_id, series_id, name):
    name = " ".join(str(name or "").split())[:SERIES_MAX_NAME]
    if not name or not _own_series(conn, user_id, series_id):
        return False
    conn.execute("UPDATE series SET name=? WHERE id=? AND user_id=?",
                 (name, series_id, user_id))
    conn.commit()
    return True


def delete_series(conn, user_id, series_id):
    """Drop the container, never the books in it. Memberships go with it."""
    if not _own_series(conn, user_id, series_id):
        return False
    conn.execute("DELETE FROM series_books WHERE series_id=?", (series_id,))
    conn.execute("DELETE FROM series WHERE id=? AND user_id=?", (series_id, user_id))
    conn.commit()
    return True


def series_of(conn, book_id):
    """Every series this book sits in, nearest name first. One is the rule for
    now; a list is what the schema already allows."""
    rows = conn.execute(
        "SELECT s.id,s.name,b.place FROM series_books b JOIN series s ON s.id=b.series_id"
        " WHERE b.book_id=? ORDER BY s.name", (book_id,)).fetchall()
    out = []
    for r in rows:
        n = conn.execute("SELECT COUNT(*) c FROM series_books WHERE series_id=?",
                         (r["id"],)).fetchone()["c"]
        # `place` is stored 0-based (cheap to renumber, safe to sort); the
        # panel and the reader think in 1-based "book 2 of 5".
        out.append({"id": r["id"], "name": r["name"], "place": (r["place"] or 0) + 1,
                    "count": n})
    return out


def _renumber(conn, series_id):
    rows = conn.execute(
        "SELECT book_id,place FROM series_books WHERE series_id=? ORDER BY place",
        (series_id,)).fetchall()
    for i, r in enumerate(rows):
        if r["place"] != i:
            conn.execute("UPDATE series_books SET place=? WHERE series_id=? AND book_id=?",
                         (i, series_id, r["book_id"]))


def set_book_series(conn, user_id, book_id, series_id):
    """Put a book in a series (appended at the end) or take it out.

    An empty series_id removes the book from every series it is in, which is
    what the panel's one dropdown means by "no series". A non-empty one wins
    over any other membership, because a book has one series this release.
    """
    own = conn.execute("SELECT id FROM books WHERE id=? AND user_id=?",
                       (book_id, user_id)).fetchone()
    if not own:
        return False
    series_id = (series_id or "").strip()
    if not series_id:
        left = [r["series_id"] for r in conn.execute(
            "SELECT series_id FROM series_books WHERE book_id=?", (book_id,))]
        conn.execute("DELETE FROM series_books WHERE book_id=?", (book_id,))
        for sid in left:                 # keep the survivors' places contiguous
            _renumber(conn, sid)
        conn.commit()
        return True
    if not _own_series(conn, user_id, series_id):
        return False
    already = conn.execute(
        "SELECT place FROM series_books WHERE series_id=? AND book_id=?",
        (series_id, book_id)).fetchone()
    conn.execute("DELETE FROM series_books WHERE book_id=? AND series_id<>?",
                 (book_id, series_id))
    if not already:
        nxt = conn.execute("SELECT COALESCE(MAX(place),-1)+1 n FROM series_books"
                           " WHERE series_id=?", (series_id,)).fetchone()["n"]
        conn.execute("INSERT INTO series_books(series_id,book_id,place) VALUES(?,?,?)",
                     (series_id, book_id, nxt))
    _renumber(conn, series_id)
    conn.commit()
    return True


def move_in_series(conn, user_id, series_id, book_id, step):
    """Move one book up or down inside its series. Neighbours swap places; the
    books at the ends stay put. Returns True when something moved."""
    if not _own_series(conn, user_id, series_id):
        return False
    rows = conn.execute(
        "SELECT book_id,place FROM series_books WHERE series_id=? ORDER BY place",
        (series_id,)).fetchall()
    ids = [r["book_id"] for r in rows]
    if book_id not in ids:
        return False
    i = ids.index(book_id)
    j = i + (1 if int(step or 0) > 0 else -1)
    if j < 0 or j >= len(ids):
        return False
    ids[i], ids[j] = ids[j], ids[i]
    for k, bid in enumerate(ids):
        conn.execute("UPDATE series_books SET place=? WHERE series_id=? AND book_id=?",
                     (k, series_id, bid))
    conn.commit()
    return True


# ---- cover art ------------------------------------------------------------
# A cover is one more blob behind a ref, named on the book row. The bytes are
# the book's own (pulled from the epub) or fetched by ISBN; the host never
# invents one. `cover_ref` doubles as the cache-busting version in the UI.

def set_cover(conn, storage, book_id, data, mime="image/jpeg"):
    ref = storage.put(bytes(data))
    conn.execute("UPDATE books SET cover_ref=?,cover_mime=? WHERE id=?",
                 (ref, mime or "image/jpeg", book_id))
    conn.commit()
    return ref


def clear_cover(conn, book_id):
    conn.execute("UPDATE books SET cover_ref='',cover_mime='' WHERE id=?", (book_id,))
    conn.commit()


def cover(conn, storage, book_id):
    """(bytes, mime) for the book's cover, or None."""
    r = conn.execute("SELECT cover_ref,cover_mime FROM books WHERE id=?",
                     (book_id,)).fetchone()
    if not r or not r["cover_ref"]:
        return None
    return storage.get(r["cover_ref"]), (r["cover_mime"] or "image/jpeg")


def chapters(conn, book_id):
    rows = conn.execute("SELECT * FROM chapters WHERE book_id=? ORDER BY idx", (book_id,))
    return [dict(r) for r in rows]


def chapter_text(conn, storage, book_id, idx):
    r = conn.execute("SELECT text_ref FROM chapters WHERE book_id=? AND idx=?",
                     (book_id, idx)).fetchone()
    if not r:
        raise KeyError("no chapter %s/%s" % (book_id, idx))
    return storage.get(r["text_ref"]).decode("utf-8")


# ---- renditions: one voiced copy of one chapter ---------------------------
# A chapter can be voiced more than once - the same chapter read by a male and
# a female voice is two renditions, both kept. `chapters` points at the newest
# (the default when no voice is named); this table is the full set the player
# offers. The key is deterministic, so a re-render of the same voice updates in
# place and a migration can re-seed without duplicating.

def rendition_key(book_id, idx, provider, voice, model, speed):
    raw = "|".join([book_id or "", str(idx), provider or "", voice or "",
                    model or "", "%.3f" % float(speed if speed is not None else 1.0)])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def add_rendition(conn, book_id, idx, provider, voice, model, speed,
                  audio_ref, audio_bytes, duration_s=None, voice_label="", pron=""):
    rk = rendition_key(book_id, idx, provider, voice, model, speed)
    conn.execute(
        "INSERT OR REPLACE INTO renditions"
        "(rk,book_id,idx,provider,voice,voice_label,model,speed,pron,"
        " audio_ref,audio_bytes,duration_s,created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rk, book_id, idx, provider or "", voice or "", voice_label or "",
         model or "", float(speed or 1.0), pron or "", audio_ref, audio_bytes,
         duration_s, now()))


def renditions(conn, book_id):
    rows = conn.execute(
        "SELECT * FROM renditions WHERE book_id=? ORDER BY idx, created_at",
        (book_id,))
    return [dict(r) for r in rows]


def rendition(conn, book_id, idx, provider="", voice="", model="", speed=None):
    """The rendition of one chapter for a named voice, or None. An empty voice
    matches any, so the newest wins (the ORDER BY picks it).

    `speed` narrows to one speed when the caller knows it. The player already
    names the exact rendition it showed the user (provider, voice, model AND
    speed); without the speed a second row for the same voice at another speed
    could be served instead - a different blob, so a different length, and a
    player that checks what it was handed would refuse to play it.
    """
    want_speed = None
    if speed not in (None, ""):
        try:
            want_speed = float(speed)
        except (TypeError, ValueError):
            want_speed = None
    for r in renditions(conn, book_id):
        if r["idx"] != idx:
            continue
        if voice and r["voice"] != voice:
            continue
        if provider and r["provider"] != provider:
            continue
        if model and r["model"] != model:
            continue
        if want_speed is not None:
            have = r["speed"]
            if abs(float(have if have is not None else 1.0) - want_speed) > 0.0005:
                continue
        if r["audio_ref"]:
            return r
    return None


def backfill_renditions(conn):
    """Seed the renditions table from chapter rows of a book voiced before this
    table existed, so it still lists its one voice. Idempotent: the key is
    deterministic and INSERT OR IGNORE skips what is already there."""
    rows = conn.execute(
        "SELECT book_id,idx,provider,voice,model,speed,audio_ref,audio_bytes,duration_s "
        "FROM chapters WHERE audio_ref IS NOT NULL").fetchall()
    for r in rows:
        rk = rendition_key(r["book_id"], r["idx"], r["provider"], r["voice"],
                           r["model"], r["speed"])
        conn.execute(
            "INSERT OR IGNORE INTO renditions"
            "(rk,book_id,idx,provider,voice,voice_label,model,speed,"
            " audio_ref,audio_bytes,duration_s,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (rk, r["book_id"], r["idx"], r["provider"] or "", r["voice"] or "",
             "", r["model"] or "", float(r["speed"] or 1.0), r["audio_ref"],
             r["audio_bytes"], r["duration_s"], now()))
    conn.commit()


# ---- removing what you hold: a voice, every voice, or the book -------------
# Deleting is per shelf. A blob is content-addressed, so the same book held by
# two people shares one physical copy, and the shared render cache keeps the
# audio of a voice that has been removed so re-voicing it stays free. A blob any
# row still names is never taken off disk. What deleting frees is THIS user's
# storage - the bytes their own shelf names - and their own copy of the book.

_REF_QUERIES = (
    ("chapters", "text_ref"),
    ("chapters", "audio_ref"),
    ("renditions", "audio_ref"),
    ("books", "cover_ref"),
    ("render_cache", "audio_ref"),
)


def ref_in_use(conn, ref):
    """True when any row anywhere still names this blob. The render cache
    counts: a cached voice is kept, so re-voicing it costs no new synth."""
    if not ref:
        return False
    for table, col in _REF_QUERIES:
        row = conn.execute("SELECT 1 FROM %s WHERE %s=? LIMIT 1" % (table, col),
                           (ref,)).fetchone()
        if row:
            return True
    return False


def gc_blobs(conn, storage, refs):
    """Delete from the backend every blob no row names any more. Returns how
    many left the disk. Housekeeping, not billing: the shelf is already freed by
    the row delete, and a shared blob survives because a row still names it."""
    freed = 0
    for ref in {r for r in refs if r}:
        if ref_in_use(conn, ref):
            continue
        try:
            storage.delete(ref)
            freed += 1
        except Exception:
            pass
    return freed


def _newest_rendition(conn, book_id, idx):
    """The newest voiced copy of one chapter, or None. Rows come ordered by
    created_at ascending, so the last match is the newest."""
    got = None
    for r in renditions(conn, book_id):
        if r["idx"] == idx and r["audio_ref"]:
            got = r
    return got


def _resync_chapter(conn, book_id, idx):
    """Point a chapter row at a voice it still has, or clear it. The chapter
    row is the 'newest voice' shortcut for a client that names none, so after a
    voice is removed it must not point at audio that is gone."""
    r = _newest_rendition(conn, book_id, idx)
    if r:
        conn.execute(
            "UPDATE chapters SET audio_ref=?,audio_bytes=?,duration_s=?,provider=?,"
            "voice=?,model=?,speed=? WHERE book_id=? AND idx=?",
            (r["audio_ref"], r["audio_bytes"], r["duration_s"], r["provider"],
             r["voice"], r["model"], r["speed"], book_id, idx))
    else:
        conn.execute(
            "UPDATE chapters SET audio_ref=NULL,audio_bytes=NULL,duration_s=NULL,"
            "provider=NULL,voice=NULL,model=NULL,speed=NULL WHERE book_id=? AND idx=?",
            (book_id, idx))


def remove_renditions(conn, storage, book_id, provider="", voice="", model=""):
    """Remove voiced copies of a book: one named voice, or (no voice given)
    every voice it has. The book and its chapter text stay. Returns
    {removed, freed}. A blob another shelf or the shared render cache still
    names stays on disk - that is exactly the shared case."""
    refs = []
    removed = 0
    for r in renditions(conn, book_id):
        if voice and r["voice"] != voice:
            continue
        if provider and r["provider"] != provider:
            continue
        if model and r["model"] != model:
            continue
        conn.execute("DELETE FROM renditions WHERE rk=?", (r["rk"],))
        if r["audio_ref"]:
            refs.append(r["audio_ref"])
        removed += 1
    for c in chapters(conn, book_id):
        _resync_chapter(conn, book_id, c["idx"])
    conn.commit()
    freed = gc_blobs(conn, storage, refs)
    return {"removed": removed, "freed": freed}


def delete_book(conn, storage, book_id):
    """Remove a book from its shelf entirely: its rows gone, then every blob
    left unnamed by every shelf taken off disk. Positions, device stops and
    listen rows belong to playback/stats and are the caller's to clear. Returns
    {freed}."""
    refs = []
    for c in chapters(conn, book_id):
        if c["text_ref"]:
            refs.append(c["text_ref"])
        if c["audio_ref"]:
            refs.append(c["audio_ref"])
    for r in renditions(conn, book_id):
        if r["audio_ref"]:
            refs.append(r["audio_ref"])
    b = conn.execute("SELECT cover_ref FROM books WHERE id=?", (book_id,)).fetchone()
    if b and b["cover_ref"]:
        refs.append(b["cover_ref"])
    conn.execute("DELETE FROM renditions WHERE book_id=?", (book_id,))
    conn.execute("DELETE FROM chapters WHERE book_id=?", (book_id,))
    conn.execute("DELETE FROM books WHERE id=?", (book_id,))
    conn.commit()
    freed = gc_blobs(conn, storage, refs)
    return {"freed": freed}
