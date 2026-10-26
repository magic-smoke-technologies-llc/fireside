"""Per-user pronunciation fixes: the words a voice gets wrong.

A rule is {"from": word, "to": respelling}. `from` is the word exactly as it
appears in the text; `to` is how it should sound, spelled out for the engine
(Hermione -> Her-my-oh-nee, Qui-Gon -> Kwai-Gon). The host applies the list to
the chapter text before any speech engine sees it, so every engine gets the fix,
including the local one that cannot take phonemes.

Rules live in named dictionaries, and a dictionary is attached at one scope:
global (every book this user has), author, series, or one book. A fic's money
glyphs are usually the author's own convention and sometimes just one story's,
and a series repeats its names book after book, which is why the words move out
of one book and into a file that can be attached, exported, and handed on.
Resolution is Book > Series > Author > Global; the narrowest statement wins.
A scope nobody could establish (no dc:creator, no series metadata) is not
guessed: those books fall into the unknown bucket, which a dictionary can be
attached to.

The old shape, one flat list in user_prefs.pron with `book`/`author` keys, is
still read once and folded into dictionaries by migrate().

Numbers are handled here too, since it is the same problem: text a voice would
misread. A comma-grouped number ("50,000") makes most engines say the commas
("five zero comma zero zero zero"), so a spoken-form pass turns those into
words. Only grouped numbers are touched: a bare year or chapter number is left
exactly as the author wrote it.
"""

import hashlib
import json
import re
import uuid

from host.db import now


_ONES = ("", "one", "two", "three", "four", "five", "six", "seven", "eight",
         "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
         "sixteen", "seventeen", "eighteen", "nineteen")
_TENS = ("", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy",
         "eighty", "ninety")
_SCALES = ((10 ** 9, "billion"), (10 ** 6, "million"), (1000, "thousand"))


def _below_thousand(n):
    out = []
    if n >= 100:
        out.append(_ONES[n // 100] + " hundred")
        n %= 100
    if n >= 20:
        t = _TENS[n // 10]
        r = n % 10
        out.append(t + ("-" + _ONES[r] if r else ""))
    elif n:
        out.append(_ONES[n])
    return " ".join(out)


def number_words(n):
    """An integer in English, up to billions. 50000 -> fifty thousand."""
    if n == 0:
        return "zero"
    parts = []
    for size, name in _SCALES:
        if n >= size:
            parts.append(_below_thousand(n // size) + " " + name)
            n %= size
    if n:
        parts.append(_below_thousand(n))
    return " ".join(p for p in parts if p)


GROUPED = re.compile(r"(?<![\d.,])\d{1,3}(?:,\d{3})+(?![\d,])")


def expand_numbers(text):
    """Say a comma-grouped number as words: 50,000 -> fifty thousand.

    Only numbers that came with group separators are converted. A year, a
    chapter number, a page reference: the author's own digits are left alone,
    because "one thousand nine hundred ninety nine" is a worse reading of a
    date than the engine's own.
    """
    if not text:
        return text
    return GROUPED.sub(lambda m: number_words(int(m.group(0).replace(",", ""))), text)


# A book with no dc:creator still has to land somewhere, or its names would
# escape every scope but the book's own. Chris's call (10-26): unknown bucket.
UNKNOWN_AUTHOR = "unknown author"
UNKNOWN_SERIES = "unknown series"

SCOPES = ("global", "author", "series", "book")
DICT_FORMAT = "fireside-pron/1"


def _scope_of(r):
    """Which scope a stored rule is in, whichever shape it arrived in."""
    r = r or {}
    if str(r.get("book") or "").strip():
        return "book", str(r["book"]).strip()
    if str(r.get("series") or "").strip():
        return "series", str(r["series"]).strip()
    if str(r.get("author") or "").strip():
        return "author", str(r["author"]).strip()
    return "global", ""


def _tidy(rules):
    """Drop blanks and duplicates within one scope, keep the first spelling."""
    out, seen = [], set()
    for r in rules or []:
        frm = str((r or {}).get("from", "")).strip()
        to = str((r or {}).get("to", "")).strip()
        if not frm or not to or frm.lower() in seen:
            continue
        seen.add(frm.lower())
        out.append({"from": frm, "to": to})
    return out


def _legacy_rules(conn, uid):
    """The old flat list in user_prefs.pron, before dictionaries existed."""
    row = conn.execute("SELECT pron FROM user_prefs WHERE user_id=?",
                       (uid or "",)).fetchone()
    raw = (row["pron"] if row else "") or ""
    try:
        rules = json.loads(raw) if raw else []
    except Exception:
        return []
    if not isinstance(rules, list):
        return []
    out = []
    for r in rules:
        frm = str((r or {}).get("from", "")).strip()
        to = str((r or {}).get("to", "")).strip()
        if frm and to:
            scope, key = _scope_of(r)
            out.append(_rule(frm, to, key if scope == "book" else "",
                             key if scope == "author" else "",
                             key if scope == "series" else ""))
    return out


def dicts(conn, uid):
    """This user's dictionaries, oldest first, rules already tidied."""
    out = []
    for r in conn.execute(
            "SELECT id,name,scope,scope_key,rules,created_at FROM pron_dicts"
            " WHERE user_id=? ORDER BY created_at,id", (uid or "",)).fetchall():
        try:
            rules = json.loads(r["rules"] or "[]")
        except Exception:
            rules = []
        out.append({"id": r["id"], "name": r["name"] or "",
                    "scope": r["scope"] or "global",
                    "scope_key": r["scope_key"] or "",
                    "rules": _tidy(rules if isinstance(rules, list) else [])})
    return out


def get_dict(conn, uid, dict_id):
    for d in dicts(conn, uid):
        if d["id"] == dict_id:
            return d
    return None


def _scope_fields(scope, key):
    scope = str(scope or "global").strip().lower()
    if scope not in SCOPES:
        scope = "global"
    key = "" if scope == "global" else str(key or "").strip()
    if scope != "global" and not key:
        # an author/series/book dict with no key would match nothing, silently.
        return None
    return scope, key


def save_dict(conn, uid, name, scope, scope_key, rules, dict_id=""):
    """Create or update one dictionary. Returns it, or None for a bad scope.

    A dictionary is one named set of words attached at one scope. Two dicts on
    the same scope are allowed: both apply, and the later one in the list wins a
    word both spell, so ordering is the user's to set by when they made it.
    """
    fields = _scope_fields(scope, scope_key)
    if not fields:
        return None
    scope, key = fields
    clean = _tidy(rules)
    blob = json.dumps(clean, ensure_ascii=False)
    name = str(name or "").strip() or ("Words" if scope == "global" else "Words")
    stamp = now()
    if dict_id:
        row = conn.execute("SELECT id FROM pron_dicts WHERE id=? AND user_id=?",
                           (dict_id, uid)).fetchone()
        if not row:
            return None
        conn.execute("UPDATE pron_dicts SET name=?,scope=?,scope_key=?,rules=?,"
                     "updated_at=? WHERE id=? AND user_id=?",
                     (name, scope, key, blob, stamp, dict_id, uid))
    else:
        dict_id = "pd_" + uuid.uuid4().hex[:16]
        conn.execute("INSERT INTO pron_dicts(id,user_id,name,scope,scope_key,rules,"
                     "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                     (dict_id, uid, name, scope, key, blob, stamp, stamp))
    conn.commit()
    return get_dict(conn, uid, dict_id)


def delete_dict(conn, uid, dict_id):
    conn.execute("DELETE FROM pron_dicts WHERE id=? AND user_id=?", (dict_id, uid))
    conn.commit()
    return True


def export_dict(conn, uid, dict_id):
    """One dictionary as a file a person can hand to someone else."""
    d = get_dict(conn, uid, dict_id)
    if not d:
        return None
    return {"format": DICT_FORMAT, "name": d["name"],
            "scope": d["scope"], "scope_key": d["scope_key"],
            "rules": d["rules"]}


def import_dict(conn, uid, payload, scope=None, scope_key=None, name=""):
    """A dictionary from a file, re-attached at this user's own scope.

    The file's scope is not trusted as a target: a dict exported at one person's
    book id or series name would point at nothing here. So the words come in and
    the scope is chosen by the importer (default: global).
    """
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            return None
    if not isinstance(payload, dict):
        return None
    rules = payload.get("rules")
    if not isinstance(rules, list):
        return None
    return save_dict(conn, uid, name or payload.get("name") or "Imported words",
                     scope or "global", scope_key, rules)


def _rule(frm, to, book="", author="", series=""):
    """A rule's stored shape. One scope at a time: book, series, author, else all."""
    book = str(book or "").strip()
    series = str(series or "").strip()
    author = str(author or "").strip()
    if book:
        return {"from": frm, "to": to, "book": book, "author": "", "series": ""}
    if series:
        return {"from": frm, "to": to, "book": "", "author": "", "series": series}
    return {"from": frm, "to": to, "book": "", "author": author, "series": ""}


def migrate(conn, uid):
    """Fold the old flat list into dictionaries once, and clear it.

    Rules that were global become one global dict; each author and each book
    keeps its own. Nothing is dropped: a rule the old shape could hold is a rule
    the new shape holds.
    """
    if not uid:
        return 0
    if conn.execute("SELECT id FROM pron_dicts WHERE user_id=? LIMIT 1",
                    (uid,)).fetchone():
        return 0
    legacy = _legacy_rules(conn, uid)
    if not legacy:
        return 0
    groups = {}
    for r in legacy:
        scope, key = _scope_of(r)
        groups.setdefault((scope, key), []).append({"from": r["from"], "to": r["to"]})
    titles = {}
    for (scope, key), rules in groups.items():
        name = "My words"
        if scope == "book":
            row = conn.execute("SELECT title FROM books WHERE id=?", (key,)).fetchone()
            name = "Words for %s" % ((row["title"] if row else "") or "this book")
        elif scope == "author":
            name = "Words for %s" % key
        elif scope == "series":
            name = "Words for %s" % key
        save_dict(conn, uid, name[:255], scope, key, rules)
        titles[(scope, key)] = name
    row = conn.execute("SELECT user_id FROM user_prefs WHERE user_id=?", (uid,)).fetchone()
    if row:
        conn.execute("UPDATE user_prefs SET pron='',updated_at=? WHERE user_id=?",
                     (now(), uid))
    else:
        conn.execute("INSERT INTO user_prefs(user_id,pron,updated_at) VALUES(?,?,?)",
                     (uid, "", now()))
    conn.commit()
    return len(groups)


def load(conn, uid):
    """Every rule this user has, one flat list, oldest dictionary first.

    Anything malformed is dropped rather than raised: one bad row must not take
    down a render.
    """
    uid = uid or ""
    migrate(conn, uid)
    out = []
    for d in dicts(conn, uid):
        for r in d["rules"]:
            out.append(_rule(r["from"], r["to"],
                             d["scope_key"] if d["scope"] == "book" else "",
                             d["scope_key"] if d["scope"] == "author" else "",
                             d["scope_key"] if d["scope"] == "series" else ""))
    return out


def save(conn, uid, rules):
    """The old whole-list write, kept so an old client still behaves.

    It lands in the dictionaries like anything else: one dict per scope in the
    given list. A dict the user named and did not include is left alone, because
    this call only owns the words it was handed.
    """
    if rules is None:                   # nothing was said, so nothing changes
        return load(conn, uid)
    groups = {}
    for r in rules or []:
        frm = str((r or {}).get("from", "")).strip()
        to = str((r or {}).get("to", "")).strip()
        if not frm or not to:
            continue
        scope, key = _scope_of(r)
        groups.setdefault((scope, key), []).append({"from": frm, "to": to})
    keep = {d["id"] for d in dicts(conn, uid)}
    existing = {(d["scope"], d["scope_key"]): d for d in dicts(conn, uid)}
    for (scope, key), items in groups.items():
        hit = existing.get((scope, key))
        save_dict(conn, uid, hit["name"] if hit else "My words", scope, key, items,
                  dict_id=hit["id"] if hit else "")
        if hit:
            keep.discard(hit["id"])
    # a scope this call emptied has to empty, or the old client's delete is a lie
    for (scope, key), d in existing.items():
        if (scope, key) not in groups and d["id"] in keep:
            delete_dict(conn, uid, d["id"])
    return load(conn, uid)


def for_book(rules, book_id="", author="", series=()):
    """The rules that apply to one book: global, author, series, then its own.

    A more specific rule wins for the same word, because that is the narrower
    statement and the user typed it on purpose.
    """
    bid = book_id or ""
    who = (author or "").strip().lower() or UNKNOWN_AUTHOR
    names = {str(s or "").strip().lower() for s in (series or ())}
    names.discard("")
    if not names:
        names = {UNKNOWN_SERIES}

    def layer(match):
        return [r for r in (rules or []) if match(r)]

    out = layer(lambda r: not r.get("book") and not r.get("series") and not r.get("author"))
    for mine in (layer(lambda r: r.get("author") and (r["author"] or "").strip().lower() == who),
                 layer(lambda r: r.get("series") and (r["series"] or "").strip().lower() in names),
                 layer(lambda r: bid and r.get("book") == bid)):
        if not mine:
            continue
        words = {r["from"].lower() for r in mine}
        out = [r for r in out if r["from"].lower() not in words] + mine
    return out


def book_author(conn, book_id):
    """The author string stored on a book, for author-scoped rules."""
    row = conn.execute("SELECT author FROM books WHERE id=?", (book_id or "",)).fetchone()
    return (row["author"] if row else "") or ""


def book_series(conn, book_id):
    """Every series name this book belongs to: the epub's own, and the shelves.

    Two sources, because they answer the same question. calibre:series is what
    the file says, and a shelf is what the reader said; a rule attached to
    either name should reach this book.
    """
    names = []
    try:
        row = conn.execute("SELECT series_name FROM books WHERE id=?",
                           (book_id or "",)).fetchone()
        if row and (row["series_name"] or "").strip():
            names.append(row["series_name"].strip())
    except Exception:                   # a database that predates the column
        pass
    try:
        for r in conn.execute(
                "SELECT s.name n FROM series_books b JOIN series s ON s.id=b.series_id"
                " WHERE b.book_id=?", (book_id or "",)).fetchall():
            if (r["n"] or "").strip():
                names.append(r["n"].strip())
    except Exception:
        pass
    out = []
    for n in names:
        if n.lower() not in {x.lower() for x in out}:
            out.append(n)
    return out


def _has_space(word):
    return any(c.isspace() for c in (word or ""))


def _prefix_amounts(text, rules):
    """A unit written BEFORE its number, read the right way round.

    Fics write money both ways: "50ʛ" (unit after) and "κ50" (unit before). A
    single-word replacement that sits directly in front of a number is a unit,
    so "κ50" becomes "fifty knut": the number said as words, then the unit. A
    multi-word replacement is not a unit, so it is left to the plain rule.
    """
    if not text:
        return text
    for r in sorted(rules or [], key=lambda r: -len(r["from"])):
        to = r["to"]
        if _has_space(to) or not to.isalpha():
            continue
        frm = re.escape(r["from"])
        if re.match(r"\w", r["from"][0]):
            frm = r"(?<![^\W\d])" + frm
        # directly adjacent only: "ʛ 30" is a glyph and a separate number, not
        # a unit in front of one.
        pat = re.compile(frm + r"(\d{1,3}(?:,\d{3})+|\d+)")
        text = pat.sub(lambda m: number_words(int(m.group(1).replace(",", "")))
                                + " " + to, text)
    return text


def _regex(rules):
    """One alternation, longest word first, whole-word where the word has edges."""
    alts = []
    for r in sorted(rules or [], key=lambda r: -len(r["from"])):
        frm = r["from"]
        e = re.escape(frm)
        if re.match(r"\w", frm[0]):
            # a letter may not precede it, but a digit may: "50\u029b" is the
            # amount symbol written after the number, which is the other half of
            # how these fics write money.
            e = r"(?<![^\W\d])" + e
        if re.match(r"\w", frm[-1]):
            # Whole-word, except that a digit may follow: a currency glyph glued
            # to an amount ("\u029b50,000") is still that glyph, and that is the
            # shape this rule exists for. A letter may not follow, so "cat" stays
            # out of "cats".
            e = e + r"(?![^\W\d])"
        alts.append(e)
    return re.compile("|".join(alts), re.I) if alts else None


def apply(text, rules):
    """Swap whole words for their say-as respelling, case-insensitive. The
    replacement goes in literally, never read back as a regex."""
    pat = _regex(rules)
    if not text or pat is None:
        return text
    repl = {r["from"].lower(): r["to"] for r in rules}

    def _swap(m):
        word = m.group(0)
        to = repl.get(word.lower(), word)
        if to is word or to == word:
            return word
        nxt = text[m.end():m.end() + 1]
        prv = text[m.start() - 1:m.start()]
        # A replacement glued to a number needs its own space on either side, or
        # the amount is said as part of the word: "galleonfifty thousand",
        # "fiftygalleon". Digits on both sides ("50ʛ") get both spaces.
        out = to
        if prv.isdigit() and to[:1].isalpha():
            out = " " + out
        if nxt.isdigit() and to[-1:].isalpha():
            out = out + " "
        return out

    return pat.sub(_swap, text)


def spoken(text, rules):
    """The text a voice is actually given: rules first, then numbers.

    One entry point so a render, a cache tag and an estimate can never disagree
    about what is being said.
    """
    return expand_numbers(apply(_prefix_amounts(text, rules), rules))


def token_for(text, rules):
    """A short tag for just the rules that match `text`, for the audio cache key.

    Only the matching rules go in, so adding a rule for a word that lives in one
    chapter does not invalidate chapters where that word never appears. Empty
    means no rule applies, and the chapter keeps whatever audio it already had.
    """
    pat = _regex(rules)
    if not pat or not text:
        return ""
    found = {m.group(0).lower() for m in pat.finditer(text)}
    use = [r for r in rules if r["from"].lower() in found]
    if not use:
        return ""
    raw = "|".join("%s>%s" % (r["from"].lower(), r["to"])
                   for r in sorted(use, key=lambda r: r["from"].lower()))
    return "p" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:6]


def impact(conn, storage, user_id, book_id=None, split_for=None):
    """Chapters already voiced that the current rules make out of date.

    For every book the user owns, look at each rendition - one voiced copy of a
    chapter, one voice. A rendition is stale when the pronunciation tag it was
    made with no longer matches the tag the rules produce for that chapter's
    text. That is the exact test the renderer uses to decide what to redo.

    `split_for(provider)` returns a provider's chunk function, so the report can
    also say how much audio is genuinely new: only the chunks with no cached
    audio for the current rules get synthesised, the rest are reused. Without a
    split function the whole chapter counts, a safe upper bound.

    Grouped by book and by (provider, voice, model, speed) so the caller can
    re-encode each voice with a real render call, scoped to just these chapters.
    """
    from host import library, jobs

    all_rules = load(conn, user_id)
    q = "SELECT id,title,author FROM books WHERE user_id=?"
    args = [user_id]
    if book_id:
        q += " AND id=?"
        args.append(book_id)
    books = []
    chapters_affected = 0
    chars_to_synth = 0
    est_seconds = 0.0
    for b in conn.execute(q, tuple(args)).fetchall():
        # a rule scoped to one book only invalidates that book's audio
        who = b["author"] if "author" in b.keys() else book_author(conn, b["id"])
        rules = for_book(all_rules, b["id"], who, book_series(conn, b["id"]))
        chapters = library.chapters(conn, b["id"])
        by_idx = {c["idx"]: c for c in chapters}
        rends = {}
        for r in library.renditions(conn, b["id"]):
            rends.setdefault(r["idx"], []).append(r)
        stale = set()
        groups = {}
        for c in chapters:
            has_default = bool(c.get("audio_ref"))
            rs = rends.get(c["idx"], [])
            if not rs and not has_default:
                continue  # never voiced: nothing to redo
            try:
                text = storage.get(c["text_ref"]).decode("utf-8")
            except Exception:
                text = ""
            tag = token_for(text, rules)
            if not rs and has_default:
                # legacy row with audio but no rendition entry; treat the
                # chapter's own columns as the one voiced copy.
                rs = [{"provider": c.get("provider") or "",
                       "voice": c.get("voice") or "", "voice_label": "",
                       "model": c.get("model") or "",
                       "speed": c.get("speed") or 1.0,
                       "pron": c.get("pron") or ""}]
            for r in rs:
                if (r.get("pron") or "") == tag:
                    continue
                prov = r.get("provider") or ""
                voice = r.get("voice") or ""
                model = r.get("model") or ""
                speed = float(r.get("speed") or 1.0)
                label = r.get("voice_label") or ""
                split = split_for(prov) if split_for else None
                if callable(split):
                    sc = jobs.stale_chars(conn, storage, text, rules, split,
                                          prov, voice, model, speed)
                else:
                    sc = len(text)
                g = groups.setdefault((prov, voice, model, speed, label),
                                      {"chapters": [], "chars_to_synth": 0})
                g["chapters"].append(c["idx"])
                g["chars_to_synth"] += sc
                stale.add(c["idx"])
        if not stale:
            continue
        rends_out = []
        book_chars = 0
        book_seconds = 0.0
        for (prov, voice, model, speed, label), g in groups.items():
            secs = g["chars_to_synth"] / jobs.synth_rate(prov)
            book_chars += g["chars_to_synth"]
            book_seconds += secs
            rends_out.append({"provider": prov, "voice": voice, "model": model,
                              "speed": speed, "voice_label": label,
                              "chapters": sorted(g["chapters"]),
                              "chars_to_synth": g["chars_to_synth"],
                              "est_seconds": round(secs, 1)})
        books.append({
            "id": b["id"], "title": b["title"],
            "chapters": [{"idx": i,
                          "title": (by_idx.get(i) or {}).get("title") or ""}
                         for i in sorted(stale)],
            "renditions": rends_out,
            "chars_to_synth": book_chars,
            "est_seconds": round(book_seconds, 1),
        })
        chapters_affected += len(stale)
        chars_to_synth += book_chars
        est_seconds += book_seconds
    return {"books": books, "books_affected": len(books),
            "chapters_affected": chapters_affected,
            "chars_to_synth": chars_to_synth,
            "est_seconds": round(est_seconds, 1)}
