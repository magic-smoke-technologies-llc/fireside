"""The v2 host, served: HTTP routes over the registry, library, jobs, ledger.

One deployment-agnostic server. Running at home and running hosted differ only
in auth mode and in whose credential is used; the routes are identical. Nothing
here imports a plugin by name - it goes through the registry.

Endpoints (JSON unless noted):
  GET  /health                              what this deployment loaded
  GET  /plugins                             readers + speech + storage, with fields
  GET  /library?user=                        the user's books
  POST /import   {user, kind, url|data_b64, filename}   reader -> book
  POST /render   {user, book, provider, voice, model, chapters?}   -> {job_id}
  GET  /job?id=                              job status
  GET  /progress?user=&book=                 per-chapter title, chars, rendered status
  GET  /text?book=&idx=                      the chapter text (story page, no audio)
  GET  /chapter?book=&idx=                   raw audio bytes (verify-before-play)
  POST /speak    {text, connection_id|provider, voice, model, speed}  free-text
                synth, straight back as audio (the voice sandbox; nothing stored)
  GET  /position?user=&book=                 current position
  POST /position {user, book, chapter_index, offset_seconds, device}
  POST /claim    {user, book, device}        take ownership; stops the other
  GET  /stop?user=&book=&device=             has this device been told to stop
  POST /ack      {user, book, device}        confirm the stop
  GET  /stats?user=                          chars voiced, books listened, time heard
  POST /listen   {user, book, seconds}       append one listen-time heartbeat
"""

import base64
import ipaddress
import json
import os
import re
import socket
import threading
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from host.host import Host
import inspect

from host import accounting, auth, billing, connections, contracts, credentials, db, draft as drafts, erase, example, grants, i18n, jobs, library, mailer, payments, playback, plans, pron, quota, ratelimit, reading, stats
from host.egress import fetch as _fetch

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI_DIR = os.path.join(BASE_DIR, "ui")
ASSETS_DIR = os.path.join(BASE_DIR, "assets")
HOST_VERSION = "2.0"


def _sale_offer(provider, speech, cred):
    """What this voice server can actually sell, or None if it can sell nothing.

    A sale connection existing is not the same as something being for sale. The
    first shape of this emitted a sale dict for any speech connection and left
    rate_per_1m_chars as a null for the client to interpret, so one capability
    was described by two fields that could disagree (Leo, 2026-10-25). The test
    here is the same question the purchase route will ask: can this plugin turn
    a dollar into characters? No characters, no sale.
    """
    def _ask(name, *args):
        fn = getattr(speech, name, None)
        return fn(*args) if callable(fn) else None

    rate = _ask("sale_rate_per_1m", cred)
    try:
        rate = float(rate) if rate is not None else None
    except (TypeError, ValueError):
        rate = None
    if rate is not None and not rate > 0:
        rate = None

    bundles = []
    for b in _ask("bundles", cred) or []:
        try:
            if b.get("usd") is not None:
                bundles.append(b)
        except AttributeError:
            continue

    try:
        buys = int(_ask("chars_for_payment", 1.0, cred) or 0)
    except (TypeError, ValueError):
        buys = 0
    if buys <= 0:
        return None            # nothing it can charge for, whatever it claims
    if rate is None and not bundles:
        return None            # chargeable, but no price it can show
    return {"provider": provider, "rate_per_1m_chars": rate,
            "currency": "USD", "bundles": bundles}


def _sale_connection_from_env():
    """The one connection that sells character capacity, from the environment.

    A deployment names it once, so turning resale on is an operator action, not
    a code change: `FIRESIDE_SALE_CONNECTION` is either JSON
    ({"connection_id": "...", "provider": "azure", "voice": "..."}) or the
    shorthand "provider:voice[:model]". Absent means nothing is sold and every
    render is pass-through, which is the honest default.
    """
    raw = (os.environ.get("FIRESIDE_SALE_CONNECTION") or "").strip()
    if not raw:
        return None
    try:
        spec = json.loads(raw)
    except ValueError:
        parts = raw.split(":")
        spec = {"provider": parts[0]}
        if len(parts) > 1:
            spec["voice"] = parts[1]
        if len(parts) > 2:
            spec["model"] = parts[2]
    return spec if isinstance(spec, dict) and spec else None


class App:
    def __init__(self, plugins_root=None, db_path=None, storage_id=None,
                 sale_connection=None, rate_limiter=None, hosted=False,
                 require_auth=False):
        self.host = Host(plugins_root)
        self.db_path = db_path
        self.conn = db.connect(db_path)
        library.backfill_renditions(self.conn)   # a book voiced before renditions still lists its voice
        jobs.sweep_stale(self.conn)              # a fresh boot has no live renderer
        # Backend selection: an explicit id, else $FIRESIDE_STORAGE, else local.
        # An unknown or unconfigured backend falls back to local so the box still
        # comes up; local is the reference and the default everywhere.
        want = storage_id or os.environ.get("FIRESIDE_STORAGE") or "local"
        if want not in self.host.storage:
            want = "local"
        elif want != "local":
            obj = self.host.storage[want].obj
            if hasattr(obj, "configured") and not obj.configured:
                want = "local"
        self.storage_id = want
        self.sale_connection = (sale_connection if sale_connection is not None
                                else _sale_connection_from_env())
        self.rate_limiter = rate_limiter
        # Two more limiters, for the doors that spend money or send mail. A
        # purchase starts a Stripe session (and a retry loop can start hundreds),
        # and register/forgot are the two unauthenticated doors that write a row
        # or send a message - the classic way a small host is turned into someone
        # else's tool. Separate from rate_limiter so a metered render does not
        # eat a buyer's checkout budget.
        self.buy_limiter = ratelimit.RateLimiter(rate_per_min=10, burst=6)
        self.door_limiter = ratelimit.RateLimiter(rate_per_min=12, burst=8)
        self.hosted = hosted
        self.require_auth = require_auth
        self.auth = auth.Auth(self.conn)

    def _uid(self, q, body):
        """The signed-in user wins over any user id the client passed in."""
        return ((q or {}).get("_user") or (body or {}).get("user")
                or (q or {}).get("user") or "")

    def _limit(self, limiter, key):
        """False when this caller has used up its allowance."""
        return limiter is None or not key or limiter.allow(key)

    def _safe_return(self, caller, base):
        """A return URL we are willing to send a paying browser back to.

        The caller used to supply it outright, which let anyone hand us a URL and
        have Stripe bounce a real, paid session id to their own site. Only our
        own origin is accepted now; anything else gets the canonical page.
        """
        base = (base or os.environ.get("FIRESIDE_BASE_URL") or "").rstrip("/")
        caller = (caller or "").strip()
        if base and (caller == base or caller.startswith(base + "/")):
            return caller
        return base

    def _idem_get(self, uid, key):
        """(status, payload) this user already got for this key, or None."""
        key = (key or "").strip()[:191]
        if not uid or not key:
            return None
        r = self.conn.execute("SELECT status, response FROM idempotency "
                              "WHERE user_id=? AND kkey=?", (uid, key)).fetchone()
        if not r:
            return None
        try:
            return int(r["status"]), json.loads(r["response"] or "{}")
        except (TypeError, ValueError):
            return None

    def _idem_put(self, uid, key, status, payload):
        """Remember a first answer so a retried request gets the same one."""
        key = (key or "").strip()[:191]
        if not uid or not key or not isinstance(payload, dict):
            return
        if status >= 500 or payload.get("_html"):
            return
        try:
            self.conn.execute(
                "INSERT INTO idempotency (user_id, kkey, status, response, created_at) "
                "VALUES (?,?,?,?,?) ON CONFLICT(user_id, kkey) DO UPDATE SET "
                "status=excluded.status, response=excluded.response",
                (uid, key, int(status), json.dumps(payload), db.now()))
            self.conn.commit()
        except Exception:
            # A cache is never allowed to fail the request it is caching.
            pass

    def _claim_payment(self, event_id, kind, uid, reference, chars=0, detail=""):
        """True the first time this provider event is applied, False after.

        One row per provider object, so a webhook retry and the browser's own
        confirm can both land and the characters are credited once. Portable
        across the two engines: a select then an insert, no upsert clause.
        """
        event_id = (event_id or "").strip()
        if not event_id:
            return False
        for _ in range(2):
            if self.conn.execute("SELECT 1 FROM payment_events WHERE id=?",
                                 (event_id,)).fetchone():
                return False
            try:
                self.conn.execute(
                    "INSERT INTO payment_events (id, kind, user_id, reference, "
                    "chars, detail, created_at) VALUES (?,?,?,?,?,?,?)",
                    (event_id, kind, uid, reference, int(chars or 0),
                     detail or "", db.now()))
                return True
            except Exception:
                continue
        return False

    @property
    def storage(self):
        return self.host.storage[self.storage_id].obj

    # ---- registry -------------------------------------------------------------

    def health(self, q, body):
        """What this deployment actually loaded, not what we meant to deploy.

        A green build and a successful deploy can both be true while the live
        box runs with a switch off: #161 was merged and shipped, and hosted was
        still false because the container set an env var the host never read.
        So the deploy carries its own name (FIRESIDE_BUILD) and the flags that
        change behaviour are reported here, where a deploy check can compare
        them against the config it just sent.
        """
        h = self.host.health()
        h["hosted"] = self.hosted
        h["require_auth"] = bool(self.require_auth)
        h["storage_backend"] = self.storage_id
        h["db_backend"] = db.backend_name()
        h["payments_configured"] = payments.configured()
        h["sale_connection"] = bool(self.sale_connection)
        h["build"] = os.environ.get("FIRESIDE_BUILD", "").strip()
        return 200, h

    def _fields(self, o):
        fn = getattr(o, "settings", None)
        if not callable(fn):
            return []
        try:
            raw = fn()
        except Exception:
            return []
        out = []
        for f in raw:                            # one shape for the UI: name + kind
            d = f.dict() if hasattr(f, "dict") else dict(f)
            if "name" not in d and "key" in d:
                d["name"] = d["key"]
            if "kind" not in d and "type" in d:
                d["kind"] = d["type"]
            out.append(d)
        return out

    def plugins(self, q, body):
        out = {"readers": [], "speech": [], "storage": []}
        for p in self.host.readers:
            out["readers"].append({"id": p.id, "label": p.label, "order": p.order,
                                   "settings": self._fields(p.obj)})
        for pid, p in self.host.speech.items():
            o = p.obj
            out["speech"].append({"id": pid, "label": p.label,
                                  "needs_key": bool(getattr(o, "needs_key", True)),
                                  "ext": getattr(o, "ext", ""),
                                  "settings": self._fields(o)})
        for pid, p in self.host.storage.items():
            out["storage"].append({"id": pid, "info": p.obj.info()})
        return 200, out

    def voices(self, q, body):
        uid = self._uid(q, body)
        provider = body.get("provider") or q.get("provider") or ""
        cid = body.get("connection_id") or q.get("connection_id") or ""
        cred = body.get("cred") or {}
        if cid:
            c = connections.get(self.conn, cid)
            if not connections.may_use(c, uid, auth.is_admin(self.conn, uid)):
                return 403, {"error": "no such connection"}
            resolved = connections.resolve(self.conn, uid, cid)
            if resolved is None:
                return 403, {"error": "connection is switched off"}
            provider, cred = c["provider"], resolved
        if provider not in self.host.speech:
            return 400, {"error": "unknown provider"}
        raw = self.host.speech[provider].obj.list_voices(cred)
        return 200, {"voices": [v.dict() if hasattr(v, "dict") else dict(v) for v in raw]}

    def ui(self, q, body):
        """Serve the browser client. The host owns its own UI; plugins ship none."""
        with open(os.path.join(UI_DIR, "index.html"), "r", encoding="utf-8") as f:
            page = f.read().replace("__HOST_VERSION__", HOST_VERSION)
        # localisable: the handler picks the language and splices the catalog in,
        # because only the request knows the cookie and Accept-Language.
        return 200, {"_html": page, "_locale": True}

    def i18n_js(self, q, body):
        """The client half of i18n: T(), data-t application, language switch.

        Cached hard: it changes only when we ship, and it is on the critical
        path of the first paint.
        """
        with open(os.path.join(UI_DIR, "i18n.js"), "r", encoding="utf-8") as f:
            src = f.read().encode("utf-8")
        return 200, {"_raw": src, "_mime": "application/javascript; charset=utf-8",
                     "_cache": "public, max-age=600"}

    def _legal(self, name):
        """A public, static legal page a store listing or payment processor can mine."""
        with open(os.path.join(UI_DIR, name), "r", encoding="utf-8") as f:
            page = f.read()
        page = (page
                .replace("__COMPANY__", os.environ.get("FIRESIDE_COMPANY", "Fireside"))
                .replace("__CONTACT__", os.environ.get("FIRESIDE_CONTACT_EMAIL",
                                                      "fireside@readfireside.com")))
        return 200, {"_html": page}

    def privacy(self, q, body):
        """The privacy policy. Public, static, and store-listing mineable."""
        return self._legal("privacy.html")

    def terms(self, q, body):
        """The terms of service. Public, static, and store-listing mineable."""
        return self._legal("terms.html")

    def delete_account(self, q, body):
        """How to erase your account. Public and static: a store listing has to
        point at a URL, and it must be reachable by someone who already
        uninstalled the app."""
        return self._legal("delete_account.html")

    def favicon(self, q, body):
        """The browser-tab icon: the same mark the install screen shows."""
        with open(os.path.join(ASSETS_DIR, "favicon.ico"), "rb") as f:
            return 200, {"_raw": f.read(), "_mime": "image/x-icon"}


    # ---- PWA ------------------------------------------------------------------
    # Static bits a browser fetches outside any session: the manifest, the
    # service worker (it must live at the root to claim the whole scope), and the
    # install icons. All are public routes, because Chrome fetches them before a
    # session exists.

    def _asset(self, name, mime):
        with open(os.path.join(ASSETS_DIR, name), "rb") as f:
            return 200, {"_raw": f.read(), "_mime": mime}

    def manifest(self, q, body):
        with open(os.path.join(UI_DIR, "manifest.webmanifest"), "rb") as f:
            return 200, {"_raw": f.read(), "_mime": "application/manifest+json"}

    def service_worker(self, q, body):
        with open(os.path.join(UI_DIR, "sw.js"), "rb") as f:
            return 200, {"_raw": f.read(), "_mime": "text/javascript; charset=utf-8"}

    def icon192(self, q, body):
        return self._asset("icon-192.png", "image/png")

    def icon512(self, q, body):
        return self._asset("icon-512.png", "image/png")

    def icon_maskable(self, q, body):
        return self._asset("icon-maskable-512.png", "image/png")

    # ---- library --------------------------------------------------------------

    def library_list(self, q, body):
        return 200, library.list_books(self.conn, self._uid(q, None))

    def preview_import(self, q, body):
        """Read the file, decide nothing, write nothing.

        The user drops a PDF, waits for the parse, and looks at what the machine
        thinks the chapters and paragraphs are before a single character is
        charged. Nothing touches the shelf here, so a bad parse costs no storage
        and leaves no half-book behind. Duplicate detection still runs, because
        finding out after the preview that you already own this book is worse
        than finding out at the start.
        """
        user = self._uid(q, body)
        req = {"kind": body.get("kind", "url"), "url": body.get("url"),
               "filename": body.get("filename"),
               "data": base64.b64decode(body["data_b64"]) if body.get("data_b64") else None}
        plugin = self._pick_reader(req)
        if plugin is None:
            return 400, self._no_reader(req)
        cred = self._source_cred(user, plugin.id, body.get("cred"))
        try:
            book = plugin.obj.read(req, self._make_fetch(plugin.obj, cred), cred)
        except contracts.Refused as e:
            return e.status, e.payload()
        parsed = (getattr(book, "meta", None) or {}).get("draft")
        if not parsed:
            # A reader that does not produce a block-level draft (epub, ao3, web)
            # gets no preview: inventing one would mean re-parsing its text here
            # and the two parsers would disagree.
            return 200, {"preview": None, "source": plugin.id,
                         "title": getattr(book, "title", ""),
                         "chapters": len(getattr(book, "chapters", [])),
                         "detail": "this reader imports directly; no preview"}
        out = drafts.draft_summary(parsed, plugin.id)
        existing = library.find_same(self.conn, user,
                                     getattr(book, "meta", None) or {}, plugin.obj)
        if existing:
            held = library.get_book(self.conn, existing) or {}
            out["existing"] = {"book_id": existing, "title": held.get("title") or ""}
        return 200, {"preview": out}

    def import_book(self, q, body):
        user = self._uid(q, body)
        req = {"kind": body.get("kind", "url"), "url": body.get("url"),
               "filename": body.get("filename"),
               "data": base64.b64decode(body["data_b64"]) if body.get("data_b64") else None}
        plugin = self._pick_reader(req)
        if plugin is None:
            return 400, self._no_reader(req)
        cred = self._source_cred(user, plugin.id, body.get("cred"))
        try:
            book = plugin.obj.read(req, self._make_fetch(plugin.obj, cred), cred)
        except contracts.Refused as e:
            # A plugin that can read the file but not turn it into a book says
            # so in its own words. That is a 422 with a reason, not a 500.
            return e.status, e.payload()
        # Whatever identity the reader named, this account either already holds
        # that work or it does not. Importing again is a replace, not a second
        # copy: two of the same book on one shelf is a mistake, not a collection.
        existing = library.find_same(self.conn, user,
                                     getattr(book, "meta", None) or {}, plugin.obj)
        if existing and _truthy(body.get("keep_both")):
            existing = None
        elif existing and not _truthy(body.get("replace")):
            held = library.get_book(self.conn, existing) or {}
            chs = library.chapters(self.conn, existing)
            return 409, {
                "error": "you already have this book",
                "detail": "this is already on your shelf as \"%s\""
                          % (held.get("title") or ""),
                "existing": {"book_id": existing,
                             "title": held.get("title") or "",
                             "chapters": len(chs),
                             "rendered": sum(1 for c in chs if c["audio_ref"])}}
        incoming = self._book_bytes(book)
        # A replace only needs room for the difference: the chapter text and
        # audio it is about to displace are already counted against this plan.
        already = library.shelf_bytes(self.conn, existing) if existing else 0
        plan = (billing.get(self.conn, user) or {}).get("plan") or ""
        blocks = billing.blocks(self.conn, user)
        # The same gate every other write uses, so an admin storage grant lifts
        # an import too. Asking quota.check_room directly here skipped the grant
        # and refused a book to an account whose limit was already lifted.
        if not self._storage_ok(user, max(0, incoming - already)):
            left = max(0, quota.allowance_bytes(plan, blocks)
                       - quota.usage(self.conn, self.storage, user)["own_bytes"])
            return 507, {"error": "not enough storage",
                         "detail": "this book needs about %d bytes; %d left in your plan"
                                   % (max(0, incoming - already), left)}
        if existing:
            swapped = library.replace_book(self.conn, self.storage, existing, book)
            self._maybe_cover(existing, book)
            out = {"book_id": existing, "title": getattr(book, "title", ""),
                   "chapters": len(getattr(book, "chapters", [])),
                   "replaced": True}
            out.update(swapped)
            return 200, out
        bid = library.add_book(self.conn, self.storage, user, book)
        self._maybe_cover(bid, book)
        return 200, {"book_id": bid, "title": getattr(book, "title", ""),
                     "chapters": len(getattr(book, "chapters", []))}

    def _example_voice(self):
        """The free voice the example is pre-rendered in: the sale connection
        and the voice named on it. None when no sale connection is configured,
        in which case the example is simply a free book to add, not a free
        listen."""
        spec = self.sale_connection or {}
        cid = spec.get("connection_id")
        if not cid:
            return None
        c = connections.get(self.conn, cid)
        if not c:
            return None
        return {"connection_id": cid, "provider": c["provider"],
                "voice": spec.get("voice") or example.DEFAULT_VOICE,
                "voice_label": c["label"] or ""}

    def add_example_book(self, q, body):
        """Put the bundled example book on this user's shelf.

        A click, not an upload: the text is bundled, so the same bytes land on
        every shelf and a single pre-render serves them all. Adding it twice
        finds the copy already there. With `render` set and a sale connection
        configured, it also starts the free voice; because the text and voice
        are already cached, that costs the reader nothing."""
        user = self._uid(q, body)
        if not user:
            return 401, {"error": "auth required"}
        bid = library.find_by_meta(self.conn, user, "example", example.EXAMPLE_MARK)
        made = False
        if not bid:
            book = example.book()
            incoming = self._book_bytes(book)
            if not self._storage_ok(user, incoming):
                return 507, {"error": "not enough storage",
                             "detail": "the example needs about %d bytes and your "
                                       "plan has no room for it" % incoming}
            bid = library.add_book(self.conn, self.storage, user, book)
            author = (book.meta or {}).get("author") or ""
            if author:
                library.set_details(self.conn, bid, author=author)
            made = True
        b = library.get_book(self.conn, bid) or {}
        out = {"book_id": bid, "title": b.get("title", ""),
               "chapters": len(library.chapters(self.conn, bid)),
               "existing": not made,
               "example_voice": self._example_voice()}
        if body.get("render") and out["example_voice"]:
            ev = out["example_voice"]
            st, r = self._start_render(user, bid, cid=ev["connection_id"],
                                       provider=ev["provider"], voice=ev["voice"],
                                       voice_label=ev["voice_label"])
            out["render_status"] = st
            out["render"] = r
        return 200, out

    @staticmethod
    def _book_bytes(book):
        """Upper-bound bytes an import would add: every chapter's text plus the
        reader's cover, if any. Not deduped against blobs the user already holds,
        so the gate may over-refuse; it must never over-commit."""
        total = 0
        for ch in getattr(book, "chapters", None) or []:
            text = ch.text if isinstance(ch.text, str) else str(ch.text)
            total += len(text.encode("utf-8"))
        cover = getattr(book, "cover", None)
        if cover:
            total += len(cover)
        return total

    def _plan(self, user_id):
        return (billing.get(self.conn, user_id) or {}).get("plan") or ""

    def _storage_ok(self, user_id, nbytes):
        """True when `nbytes` of new storage fits this user's plan. Reads only;
        the caller refuses the write. An admin grant lifts the check entirely."""
        if grants.storage_unlimited(self.conn, user_id):
            return True
        return quota.check_room(self.conn, self.storage, user_id,
                                self._plan(user_id), nbytes,
                                billing.blocks(self.conn, user_id))

    # ---- cover art -----------------------------------------------------------
    # A book's cover is its own image when the reader handed one over (an epub
    # almost always carries it), otherwise it is fetched by ISBN. Both paths are
    # generic: the host has no branch on which reader produced the book.

    COVER_MAX_BYTES = 8 * 1024 * 1024
    _COVER_URLS = ("https://covers.openlibrary.org/b/isbn/{isbn}-L.jpg?default=false",)

    def _maybe_cover(self, book_id, book):
        data = getattr(book, "cover", None)
        mime = getattr(book, "cover_mime", "") or "image/jpeg"
        if not data:
            meta = getattr(book, "meta", None) or {}
            isbn = (meta.get("isbn") if isinstance(meta, dict) else "") or ""
            got = self._cover_by_isbn(isbn)
            if got:
                data, mime = got
        if not data:
            return
        try:
            library.set_cover(self.conn, self.storage, book_id, data, mime)
        except Exception:               # a cover is a nicety; never fail an import for it
            pass

    def _cover_by_isbn(self, isbn):
        """Fetch a cover for an ISBN from a keyless public source, or None."""
        isbn = re.sub(r"[^0-9Xx]", "", isbn or "")
        if not re.fullmatch(r"\d{9}[\dXx]|\d{13}", isbn or ""):
            return None
        for tpl in self._COVER_URLS:
            url = tpl.format(isbn=isbn)
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": "fireside/2 (cover lookup)"})
                with urllib.request.urlopen(req, timeout=15) as r:
                    ctype = (r.headers.get("Content-Type") or "").split(";")[0]
                    data = r.read(self.COVER_MAX_BYTES)
                if data and len(data) > 100:
                    return data, (ctype if ctype.startswith("image/") else "image/jpeg")
            except Exception:
                continue
        return None

    def cover_get(self, q, body):
        got = library.cover(self.conn, self.storage, q.get("book", ""))
        if not got:
            return 404, {"error": "no cover"}
        data, mime = got
        return 200, {"_raw": data, "_mime": mime, "checksum": ""}

    def put_cover(self, q, body):
        book_id = body.get("book", "")
        book = library.get_book(self.conn, book_id)
        if not book:
            return 404, {"error": "no such book"}
        owner = book["user_id"]
        url = (body.get("url") or "").strip()
        if url:
            got = self._cover_from_url(url)
            if not got:
                return 400, {"error": "could not fetch an image from that URL"}
            data, mime = got
            if not self._storage_ok(owner, len(data)):
                return 507, {"error": "not enough storage",
                             "detail": "your plan has no room for this cover"}
            library.set_cover(self.conn, self.storage, book_id, data, mime)
            return 200, {"ok": True, "cover": library.get_book(self.conn, book_id)["cover"]}
        raw = body.get("data_b64") or ""
        try:
            data = base64.b64decode(raw) if raw else b""
        except Exception:
            return 400, {"error": "bad image data"}
        if not data:
            return 400, {"error": "no image"}
        if len(data) > self.COVER_MAX_BYTES:
            return 413, {"error": "image is too large"}
        if not self._storage_ok(owner, len(data)):
            return 507, {"error": "not enough storage",
                         "detail": "your plan has no room for this cover"}
        library.set_cover(self.conn, self.storage, book_id, data,
                          body.get("mime") or "image/jpeg")
        return 200, {"ok": True, "cover": library.get_book(self.conn, book_id)["cover"]}

    def clear_cover(self, q, body):
        book_id = body.get("book", "")
        if not library.get_book(self.conn, book_id):
            return 404, {"error": "no such book"}
        library.clear_cover(self.conn, book_id)
        return 200, {"ok": True, "cover": ""}

    def _owned_book(self, uid, book_id):
        """The book if this user holds it, else (None, an error response)."""
        b = library.get_book(self.conn, book_id)
        if not b:
            return None, (404, {"error": "no such book"})
        if b["user_id"] != uid:
            return None, (403, {"error": "not yours to remove"})
        return b, None

    def remove_renditions(self, q, body):
        """Remove one voice from a book, or every voice it has. The book and
        its text stay; only the named voice's audio goes (a shared copy on
        another shelf is kept)."""
        uid = self._uid(q, body)
        book_id = body.get("book", "")
        _b, err = self._owned_book(uid, book_id)
        if err:
            return err
        if jobs.active_for_book(self.conn, book_id):
            return 409, {"error": "a conversion for this book is still running",
                         "detail": "cancel it, then remove the voice"}
        voice = (body.get("voice") or "").strip()
        provider = (body.get("provider") or "").strip()
        model = (body.get("model") or "").strip()
        all_voices = bool(body.get("all")) or not voice
        out = library.remove_renditions(
            self.conn, self.storage, book_id,
            provider="" if all_voices else provider,
            voice="" if all_voices else voice,
            model="" if all_voices else model)
        return 200, {"ok": True, "removed": out["removed"], "freed": out["freed"],
                     "scope": "all" if all_voices else "voice",
                     "storage": quota.usage(self.conn, self.storage, uid)}

    def delete_book(self, q, body):
        """Remove a book from the shelf: its rows, then any blob no other shelf
        (or the render cache) still names. Its position, device stops and listen
        rows for this user go with it."""
        uid = self._uid(q, body)
        book_id = body.get("book", "")
        b, err = self._owned_book(uid, book_id)
        if err:
            return err
        if jobs.active_for_book(self.conn, book_id):
            return 409, {"error": "a conversion for this book is still running",
                         "detail": "cancel it, then delete the book"}
        title = b.get("title", "")
        out = library.delete_book(self.conn, self.storage, book_id)
        playback.discard_book(self.conn, uid, book_id)
        stats.discard_book(self.conn, uid, book_id)
        return 200, {"ok": True, "title": title, "freed": out["freed"],
                     "storage": quota.usage(self.conn, self.storage, uid)}

    # a cover URL is user input, so it only ever reaches the public internet:
    # http(s), an image content-type (or a recognised image signature), and a
    # host that does not resolve into a private / loopback / link-local range.
    _IMAGE_MAGIC = ((b"\xff\xd8\xff", "image/jpeg"),
                    (b"\x89PNG\r\n\x1a\n", "image/png"),
                    (b"GIF87a", "image/gif"), (b"GIF89a", "image/gif"),
                    (b"RIFF", "image/webp"))

    def _url_allowed(self, url):
        """True only for an http(s) URL whose host resolves to a public address.
        A cover URL is user input, so it must never reach this box's own
        network or a file on it."""
        try:
            p = urllib.parse.urlsplit(url)
        except Exception:
            return False
        if p.scheme not in ("http", "https") or not p.hostname:
            return False
        try:
            infos = socket.getaddrinfo(p.hostname, p.port or
                                       (443 if p.scheme == "https" else 80))
        except Exception:
            return False
        for info in infos:
            try:
                ip = ipaddress.ip_address(info[4][0])
            except ValueError:
                return False
            if (ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
                return False
        return True

    def _cover_from_url(self, url):
        if not self._url_allowed(url):
            return None
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "fireside/2 (cover fetch)"})
            with urllib.request.urlopen(req, timeout=20) as r:
                ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                data = r.read(self.COVER_MAX_BYTES + 1)
        except Exception:
            return None
        if not data or len(data) > self.COVER_MAX_BYTES:
            return None
        sniff = ""
        for magic, m in self._IMAGE_MAGIC:
            if data.startswith(magic):
                sniff = m
                break
        if ctype.startswith("image/"):
            mime = ctype
        elif sniff:
            mime = sniff
        else:
            return None
        return data, mime

    def cover_auto(self, q, body):
        book_id = body.get("book", "")
        b = library.get_book(self.conn, book_id)
        if not b:
            return 404, {"error": "no such book"}
        isbn = b.get("isbn") or body.get("isbn") or (b.get("meta") or {}).get("isbn") or ""
        got = self._cover_by_isbn(isbn)
        if not got:
            return 404, {"error": "no cover found" + (" for that ISBN" if isbn else "")}
        data, mime = got
        library.set_cover(self.conn, self.storage, book_id, data, mime)
        return 200, {"ok": True, "cover": library.get_book(self.conn, book_id)["cover"]}

    # ---- book details ---------------------------------------------------------
    # An ISBN is the key to a book's public metadata. Given one, the settings
    # panel can fill in author, description, publisher and date without the
    # reader having typed any of it. OpenLibrary is keyless, so this works on a
    # fresh install. Nothing here overwrites the book's own title.

    DETAILS_MAX_CHARS = 4000
    _OL_EDITION = "https://openlibrary.org/isbn/{isbn}.json"
    _OL_KEY = "https://openlibrary.org{key}.json"

    def _get_json(self, url):
        req = urllib.request.Request(
            url, headers={"User-Agent": "fireside/2 (book details)"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read(4 * 1024 * 1024).decode("utf-8", "replace"))

    @staticmethod
    def _norm_isbn(isbn):
        return re.sub(r"[^0-9Xx]", "", str(isbn or ""))

    @staticmethod
    def _valid_isbn(isbn):
        return bool(re.fullmatch(r"\d{9}[\dXx]|\d{13}", isbn or ""))

    def _details_by_isbn(self, isbn):
        """Public metadata for an ISBN from OpenLibrary, or None."""
        isbn = self._norm_isbn(isbn)
        if not self._valid_isbn(isbn):
            return None
        try:
            ed = self._get_json(self._OL_EDITION.format(isbn=isbn))
        except Exception:
            return None
        out = {"isbn": isbn}
        title = ed.get("title") or ""
        if title and ed.get("subtitle"):
            title += ": " + ed["subtitle"]
        if title:
            out["title"] = title
        names = []
        for a in (ed.get("authors") or [])[:6]:
            key = a.get("key") if isinstance(a, dict) else None
            if not key:
                continue
            try:
                n = self._get_json(self._OL_KEY.format(key=key)).get("name")
            except Exception:
                n = ""
            if n:
                names.append(n)
        if not names and ed.get("by_statement"):
            names = [ed["by_statement"]]
        if names:
            out["author"] = ", ".join(names)
        pubs = [p for p in (ed.get("publishers") or []) if p][:3]
        if pubs:
            out["publisher"] = ", ".join(pubs)
        if ed.get("publish_date"):
            out["published"] = str(ed["publish_date"])
        desc = ed.get("description")
        if not desc:
            works = ed.get("works") or []
            if works and isinstance(works[0], dict) and works[0].get("key"):
                try:
                    desc = self._get_json(
                        self._OL_KEY.format(key=works[0]["key"])).get("description")
                except Exception:
                    desc = None
        if isinstance(desc, dict):
            desc = desc.get("value")
        if desc:
            out["description"] = str(desc).strip()[:self.DETAILS_MAX_CHARS]
        return out

    def fetch_details(self, q, body):
        """Look a book up by ISBN and store what we learn. An ISBN in the body
        is saved first (the panel just typed one); otherwise the stored one is
        used. The cover is fetched too, but only when the book has none."""
        book_id = body.get("book", "")
        b = library.get_book(self.conn, book_id)
        if not b:
            return 404, {"error": "no such book"}
        isbn = self._norm_isbn(body.get("isbn"))
        if isbn:
            library.set_details(self.conn, book_id, isbn=isbn)
            b = library.get_book(self.conn, book_id)
        else:
            isbn = b.get("isbn") or ""
        if not self._valid_isbn(isbn):
            return 400, {"error": "that does not look like an ISBN"}
        details = self._details_by_isbn(isbn)
        if not details:
            return 404, {"error": "no details found for that ISBN"}
        fields = {k: details[k] for k in
                  ("author", "publisher", "published", "description")
                  if details.get(k)}
        if not b.get("title") and details.get("title"):
            fields["title"] = details["title"]
        if fields:
            library.set_details(self.conn, book_id, **fields)
        if not b.get("cover"):
            got = self._cover_by_isbn(isbn)
            if got:
                try:
                    library.set_cover(self.conn, self.storage, book_id, got[0], got[1])
                except Exception:
                    pass
        return 200, {"ok": True, "book": self._book_payload(book_id)}

    def _book_payload(self, book_id):
        b = library.get_book(self.conn, book_id) or {}
        return {"id": book_id, "title": b.get("title", ""),
                "author": b.get("author", ""), "cover": b.get("cover", ""),
                "isbn": b.get("isbn", ""), "publisher": b.get("publisher", ""),
                "published": b.get("published", ""),
                "description": b.get("description", ""),
                "user_edited": b.get("user_edited", 0),
                "series": library.series_of(self.conn, book_id)}

    # ---- metadata + series ----------------------------------------------------
    # Two calls with a sharp line between them. /book/details looks facts up by
    # ISBN; /book/meta is the reader typing over them. Only the second raises
    # user_edited, which is what makes a later lookup stop being authoritative.

    def book_meta_set(self, q, body):
        book_id = body.get("book", "")
        b = library.get_book(self.conn, book_id)
        if not b or b.get("user_id") != self._uid(q, body):
            return 404, {"error": "no such book"}
        fields = {}
        for k in ("title", "author"):
            if k in body:
                fields[k] = " ".join(str(body[k] or "").split())[:255]
        if not fields:
            return 400, {"error": "nothing to save"}
        if "title" in fields and not fields["title"]:
            return 400, {"error": "a book needs a title"}
        library.set_user_details(self.conn, book_id, **fields)
        return 200, {"ok": True, "book": self._book_payload(book_id)}

    def series_list(self, q, body):
        return 200, {"series": library.list_series(self.conn, self._uid(q, body))}

    def series_save(self, q, body):
        """create / rename / delete. A series is a local container; deleting it
        never deletes a book."""
        uid = self._uid(q, body)
        op = (body.get("op") or "create").strip()
        if op == "create":
            sid = library.create_series(self.conn, uid, body.get("name"))
            if not sid:
                return 400, {"error": "a series needs a name"}
        elif op == "rename":
            if not library.rename_series(self.conn, uid, body.get("id", ""), body.get("name")):
                return 404, {"error": "no such series"}
        elif op == "delete":
            if not library.delete_series(self.conn, uid, body.get("id", "")):
                return 404, {"error": "no such series"}
        else:
            return 400, {"error": "unknown op"}
        return 200, {"ok": True, "series": library.list_series(self.conn, uid)}

    def book_series_set(self, q, body):
        """Put this book in a series (at the end) or take it out."""
        book_id = body.get("book", "")
        if not library.set_book_series(self.conn, self._uid(q, body), book_id,
                                       body.get("series", "")):
            return 404, {"error": "no such book or series"}
        return 200, {"ok": True, "book": self._book_payload(book_id)}

    def series_move(self, q, body):
        """Up or down one place inside the series. `dir` is -1 or 1."""
        series_id = body.get("series", "")
        try:
            step = int(body.get("dir", 0))
        except (TypeError, ValueError):
            step = 0
        moved = library.move_in_series(self.conn, self._uid(q, body), series_id,
                                      body.get("book", ""), step)
        return 200, {"ok": True, "moved": bool(moved),
                     "book": self._book_payload(body.get("book", ""))}

    @staticmethod
    def _no_reader(req):
        """Why nothing claimed this upload, in words a person can act on.

        "no reader matched" is true and useless. If the name says a format we
        do read, the file is the problem, not the format.
        """
        name = (req.get("filename") or "").lower()
        for ext, what in ((".pdf", "a PDF"), (".docx", "a Word document"),
                          (".epub", "an EPUB")):
            if name.endswith(ext):
                return {"error": "could not read this file",
                        "detail": "this upload does not open as %s. It may be "
                                  "truncated or renamed." % what}
        return {"error": "no reader matched",
                "detail": "nothing here reads that file type"}

    def _pick_reader(self, req):
        for p in self.host.readers:
            try:
                if p.obj.matches(req):
                    return p
            except Exception:
                continue
        return None

    def _make_fetch(self, o, cred):
        hosts = tuple(o.credential_hosts() or []) if callable(
            getattr(o, "credential_hosts", None)) else ()
        tr = o.transport() if callable(getattr(o, "transport", None)) else None

        def f(url, **kw):
            return _fetch(url, cred=cred, credential_hosts=hosts, transport=tr)
        return f

    def _source_cred(self, user, source_id, client_cred=None):
        """The credential a source reads with. The server holds it; a value the
        client sent for this source wins over a stored one (a fresh paste), and
        otherwise the stored value fills in. A reader's login never has to live
        in a browser."""
        cred = dict(client_cred or {})
        stored = credentials.get_credential(self.conn, user, "source:" + source_id)
        for k, v in (stored or {}).items():
            if v not in (None, ""):
                cred.setdefault(k, v)
        return cred

    # ---- render ---------------------------------------------------------------

    def render(self, q, body):
        user = self._uid(q, body)
        return self._start_render(
            user, body["book"],
            cid=body.get("connection_id") or "",
            provider=body.get("provider", ""),
            voice=body.get("voice", ""),
            voice_label=body.get("voice_label", ""),
            model=body.get("model", ""),
            speed=float(body.get("speed", 1.0) or 1.0),
            only=body.get("chapters"),
            client_cred=body.get("cred"))

    def _start_render(self, user, book_id, cid="", provider="", voice="", model="",
                      speed=1.0, only=None, voice_label="", client_cred=None):
        cred = None
        if cid:
            c = connections.get(self.conn, cid)
            if not connections.may_use(c, user, auth.is_admin(self.conn, user)):
                return 403, {"error": "no such connection"}
            cred = connections.resolve(self.conn, user, cid)
            if cred is None:
                return 403, {"error": "connection is switched off"}
            provider = c["provider"]
        if provider not in self.host.speech:
            return 400, {"error": "unknown provider"}
        if cred is None:
            cred = client_cred or credentials.get_credential(self.conn, user, provider)
        if self.rate_limiter is not None and not self.rate_limiter.allow(user):
            return 429, {"error": "rate limited"}
        speech = self.host.speech[provider].obj
        rows = library.chapters(self.conn, book_id)
        est = sum(c["chars"] or 0 for c in rows)
        todo = [c["idx"] for c in rows]
        if only is not None:
            todo = [i for i in todo if i in set(only)]
        todo_chars = sum(c["chars"] or 0 for c in rows if c["idx"] in set(todo))
        est_bytes = jobs.estimate(todo_chars, provider)["audio_bytes"]
        if not self._storage_ok(user, est_bytes):
            return 507, {"error": "not enough storage",
                         "detail": "this render needs about %d bytes of audio and "
                                   "your plan has no room for it" % est_bytes}
        sale = accounting.sale_connection_matches(self.sale_connection, provider, voice,
                                                  model, cid)
        # What this run actually has to synthesise: a chapter already voiced in
        # this exact voice (and rules) is a cache hit and costs the buyer
        # nothing. The whole-book estimate is the storage question, not the
        # money question, so the two are kept apart.
        rules = pron.for_book(pron.load(self.conn, user), book_id,
                              pron.book_author(self.conn, book_id))
        fresh = jobs.stale_book_chars(self.conn, self.storage, speech, book_id, todo,
                                      rules, voice, model, speed)
        res_id = None
        if sale and not grants.credits_unlimited(self.conn, user):
            quote = speech.quote({"chars_est": fresh, "cred": cred, "sale": True})
            if not getattr(quote, "eligible", True):
                return 402, {"error": "not eligible",
                             "reason": getattr(quote, "refusal_reason", "")}
            price = float(getattr(quote, "price", 0.0) or 0.0)
            if price > 0:
                try:
                    res_id = accounting.reserve(self.conn, user, price,
                                                getattr(quote, "currency", "USD"))
                except accounting.Insufficient as e:
                    # The numbers travel with the refusal. The two words on their
                    # own leave a reader with no idea how far short they are, and
                    # the screen can only print what it is sent.
                    return 402, {"error": "insufficient balance",
                                 "needed": price,
                                 "available": accounting.available(self.conn, user),
                                 "detail": str(e)}
        jid = jobs.start_job(self.conn, user, book_id, speech.id, voice, model,
                             total=len(todo), status="queued")
        opts = {"model": model, "speed": speed}

        def _work():
            # A render can run for minutes; it gets its own DB connection so it
            # never blocks the request thread and the client can poll /job.
            c = db.connect(self.db_path)
            try:
                jobs.render_book(c, self.storage, speech, user, book_id, voice,
                                 model, speed, cred=cred, opts=opts, only=only,
                                 job_id=jid, voice_label=voice_label)
                if sale and res_id:
                    # Charge only the characters this run actually synthesised:
                    # what is still stale after the run is what was never voiced
                    # (a chapter that was already cached, or one that failed).
                    try:
                        remaining = jobs.stale_book_chars(
                            c, self.storage, speech, book_id, todo, rules,
                            voice, model, speed)
                    except Exception:
                        remaining = 0
                    actual = max(0, fresh - remaining)
                    rep = speech.report({"chars_actual": actual, "cred": cred,
                                         "job_id": jid, "sale": True})
                    accounting.settle(c, res_id, rep)
            finally:
                c.close()

        threading.Thread(target=_work, daemon=True).start()
        return 200, {"job_id": jid, "total": len(todo)}

    # ---- credits (resold speech capacity, counted in characters) -------------
    # The sale connection's plugin is the authority on the rate and on how many
    # characters a payment buys; the host only holds the per-user character
    # balance and moves it. A metered run holds its estimate, the plugin reports
    # the real count, the host settles. Nothing here knows what a character costs.

    def _sale_connection(self, uid):
        """(provider, speech plugin, stored cred) for the designated sale
        connection. Pricing reads the connection's STORED data, never a user's
        override: a buyer must not be able to edit the rate they are charged."""
        spec = self.sale_connection or {}
        c = None
        cid = spec.get("connection_id")
        if cid:
            c = connections.get(self.conn, cid)
        elif spec.get("provider"):
            for cand in connections.list_for(self.conn, uid):
                if cand["provider"] == spec["provider"]:
                    c = cand
                    break
        if not c:
            return None, None, None
        p = self.host.speech.get(c["provider"])
        if p is None:
            return None, None, None
        return c["provider"], p.obj, dict(c["data"])

    def credits(self, q, body):
        """The signed-in user's character balance, plus what a sale connection
        would charge. Read-only: buying happens at /credits/purchase."""
        uid = self._uid(q, body)
        out = {
            "balance": accounting.balance(self.conn, uid),
            "reserved": accounting.reserved_total(self.conn, uid),
            "available": accounting.available(self.conn, uid),
            "unit": "chars",
            "payments_configured": payments.configured(),
            "sale": None,
        }
        provider, speech, cred = self._sale_connection(uid)
        if speech is not None:
            out["sale"] = _sale_offer(provider, speech, cred)
        return 200, out

    def credits_purchase(self, q, body):
        """Buy character capacity. Held until a payment provider is configured.

        The plugin sets how many characters a dollar amount buys (rounded down),
        and that number is pinned in the checkout metadata at the moment the
        buyer is quoted it - the server never re-derives the count later from a
        rate that may have moved. Card entry happens on the provider's hosted
        page; the host sees only a session id, and credits once that session
        reads back paid.
        """
        uid = self._uid(q, body)
        if not uid:
            return 401, {"error": "auth required"}
        if not self._limit(self.buy_limiter, uid):
            return 429, {"error": "rate limited"}
        idem = (q or {}).get("_idem", "")
        hit = self._idem_get(uid, idem)
        if hit:
            return hit
        provider, speech, cred = self._sale_connection(uid)
        if speech is None:
            return 400, {"error": "no sale connection configured"}
        try:
            usd = float((body or {}).get("usd") or 0)
        except (TypeError, ValueError):
            return 400, {"error": "bad amount"}
        if usd <= 0:
            return 400, {"error": "amount must be greater than zero"}
        pp = payments.get()
        if pp is None:
            return 503, {"error": "payments not configured",
                         "detail": "purchases are held until a payment provider lands"}
        to_chars = getattr(speech, "chars_for_payment", None)
        chars = int(to_chars(usd, cred)) if callable(to_chars) else 0
        if chars <= 0:
            return 400, {"error": "amount too small to buy characters"}
        hosted = getattr(pp, "purchase_checkout", None)
        if callable(hosted):
            co = _with_idem(hosted, usd,
                            {"user": uid, "provider": provider, "chars": chars,
                             "return_url": self._safe_return(
                                 (body or {}).get("return_url"),
                                 (q or {}).get("_base", ""))}, idem=idem)
            if not getattr(co, "ok", False):
                return 402, {"error": "checkout refused",
                             "detail": getattr(co, "detail", "")}
            payload = {"ok": True, "status": "awaiting_payment", "chars": chars,
                       "usd": round(usd, 2), "url": co.url,
                       "reference": co.reference,
                       "balance": accounting.balance(self.conn, uid)}
            self._idem_put(uid, idem, 200, payload)
            return 200, payload
        # A provider with no hosted checkout still has charge(): one call, and
        # the money either settled or it did not. Nothing is credited otherwise.
        receipt = _with_idem(pp.charge, usd,
                             {"user": uid, "provider": provider, "chars": chars},
                             idem=idem)
        if not receipt.ok:
            return 402, {"error": "payment refused", "detail": receipt.detail}
        if not self._claim_payment(receipt.reference, "characters", uid,
                                   receipt.reference, chars, "direct charge"):
            return 409, {"error": "this payment was already credited",
                         "reference": receipt.reference,
                         "balance": accounting.balance(self.conn, uid)}
        total = accounting.topup(self.conn, uid, chars, currency="chars",
                                 note="purchase %.2f USD -> %d chars (%s)"
                                      % (usd, chars, receipt.reference))
        payload = {"ok": True, "chars": chars, "balance": total,
                   "reference": receipt.reference}
        self._idem_put(uid, idem, 200, payload)
        return 200, payload

    def credits_confirm(self, q, body):
        """The browser came back from a character purchase: read it back.

        Same honesty rule as the tier: ok only when the provider says the money
        settled. The session must belong to the caller, and a session that has
        already been credited credits nothing more - so the 200 this returns is
        the same one on a reload.
        """
        uid = self._uid(q, body)
        if not uid:
            return 401, {"error": "auth required"}
        pp = payments.get()
        fn = getattr(pp, "purchase_confirm", None) if pp is not None else None
        if not callable(fn):
            return 503, {"error": "payments not configured"}
        ref = (body or {}).get("reference") or q.get("reference") or ""
        p = fn(ref)
        if not getattr(p, "ok", False):
            return 402, {"error": "not confirmed", "detail": getattr(p, "detail", "")}
        if getattr(p, "user", "") != uid:
            return 403, {"error": "that purchase belongs to another account"}
        fresh = self._claim_payment(p.reference, "characters", uid, p.reference,
                                    p.chars, "checkout confirmed")
        if fresh and p.chars:
            accounting.topup(self.conn, uid, p.chars, currency="chars",
                             note="purchase %d chars (%s)" % (p.chars, p.reference))
        return 200, {"ok": True, "chars": p.chars, "credited": fresh,
                     "balance": accounting.balance(self.conn, uid),
                     "reference": p.reference}

    # ---- billing (the recurring platform tier) -------------------------------
    # The provider is the authority on plans and on whether a checkout is paid.
    # The host records only what the provider reported.

    def billing_get(self, q, body):
        uid = self._uid(q, body)
        pp = payments.get()
        plans_list = []
        fn = getattr(pp, "plans", None)
        if callable(fn):
            plans_list = [p.dict() if hasattr(p, "dict") else dict(p) for p in fn()]
        return 200, {"subscription": billing.get(self.conn, uid) if uid else None,
                     "plans": plans_list,
                     "payments_configured": payments.configured()}

    def plans_get(self, q, body):
        """The offering, for the plan screen: the tiers and the storage add-on,
        with whatever prices a payment provider reports (None until one is set)."""
        return 200, plans.catalog(payments.get())

    def storage_get(self, q, body):
        """The signed-in user's storage: the bytes their own shelf holds and how
        much their plan plus any storage blocks include. A held book bills its
        own bytes; there is no sharing discount and nothing is auto-released.
        Read-only."""
        uid = self._uid(q, body)
        sub = billing.get(self.conn, uid) if uid else None
        plan = (sub or {}).get("plan") or ""
        blocks = billing.blocks(self.conn, uid) if uid else 0
        u = quota.usage(self.conn, self.storage, uid) if uid else {
            "user_id": uid, "own_bytes": 0, "total_bytes": 0, "blobs": 0}
        allowed = quota.allowance_bytes(plan, blocks)
        unlimited = bool(uid) and grants.storage_unlimited(self.conn, uid)
        u.update({"plan": plan, "storage_blocks": blocks,
                  "block_bytes": quota.STORAGE_BLOCK_BYTES,
                  "allowance_bytes": allowed,
                  # An admin grant lifts the refusal; the screen says so instead
                  # of showing a shelf that is over a limit nobody enforces.
                  "unlimited": unlimited,
                  "remaining_bytes": (None if unlimited
                                      else max(0, allowed - u["own_bytes"]))})
        return 200, u

    def billing_checkout(self, q, body):
        uid = self._uid(q, body)
        if not uid:
            return 401, {"error": "auth required"}
        pp = payments.get()
        fn = getattr(pp, "checkout", None)
        if not callable(fn):
            return 503, {"error": "payments not configured",
                         "detail": "the platform tier is held until a payments provider lands"}
        if not self._limit(self.buy_limiter, uid):
            return 429, {"error": "rate limited"}
        idem = (q or {}).get("_idem", "")
        hit = self._idem_get(uid, idem)
        if hit:
            return hit
        body = body or {}
        plan = body.get("plan") or q.get("plan") or ""
        items = body.get("items")
        meta = {"user": uid,
                "return_url": self._safe_return(body.get("return_url"),
                                                (q or {}).get("_base", ""))}
        if isinstance(items, list) and items:
            clean = []
            for it in items:
                if not isinstance(it, dict):
                    return 400, {"error": "bad cart item"}
                pid = str(it.get("plan") or "").strip()
                if not pid:
                    return 400, {"error": "cart item has no plan"}
                try:
                    qty = max(1, int(it.get("quantity") or 1))
                except (TypeError, ValueError):
                    return 400, {"error": "bad cart quantity"}
                clean.append({"plan": pid, "quantity": qty})
            meta["items"] = clean
            if not plan:
                plan = clean[0]["plan"]
        else:
            try:
                quantity = int(body.get("quantity") or q.get("quantity") or 1)
            except (TypeError, ValueError):
                quantity = 1
            meta["quantity"] = max(1, quantity)
        co = _with_idem(fn, plan, meta, idem=idem)
        if not getattr(co, "ok", False):
            return 402, {"error": "checkout refused",
                         "detail": getattr(co, "detail", "")}
        payload = {"url": co.url, "reference": co.reference}
        self._idem_put(uid, idem, 200, payload)
        return 200, payload

    def _apply_subscription(self, uid, sub, ref, provider_id):
        """Write what the provider reported into the rows the host reads.

        A cart applies each item to its own row; a single purchase is either the
        tier or the storage add-on. Idempotent by construction (both writers are
        upserts), so the browser confirm and the webhook landing together - in
        either order - leave the same rows, never a doubled one.
        """
        ids = {"provider": provider_id, "reference": ref,
               "current_period_end": sub.current_period_end,
               "customer": getattr(sub, "customer", "") or "",
               "subscription_id": getattr(sub, "subscription_id", "") or ""}
        items = getattr(sub, "items", None)
        if items:
            applied = []
            for it in items:
                pid = (it or {}).get("plan") or ""
                qty = int((it or {}).get("quantity") or 1)
                if pid == quota.STORAGE_ADDON:
                    row = billing.upsert_addon(
                        self.conn, uid, quota.STORAGE_ADDON, qty, sub.status,
                        **ids)
                    applied.append({"addon": row})
                elif pid:
                    row = billing.upsert(self.conn, uid, pid, sub.status, **ids)
                    applied.append({"subscription": row})
            return {"applied": applied}
        # An add-on does not replace the tier - it adds capacity to it.
        if getattr(sub, "plan", "") == quota.STORAGE_ADDON:
            return {"addon": billing.upsert_addon(
                self.conn, uid, quota.STORAGE_ADDON,
                getattr(sub, "quantity", 0) or 1, sub.status, **ids)}
        if not getattr(sub, "plan", ""):
            # a paid session that names no tier: a character purchase, or a
            # shape we do not understand. Either way a blank tier row is not a
            # subscription, and writing one would grant a free platform plan.
            return {"ignored": "checkout names no plan"}
        return {"subscription": billing.upsert(
            self.conn, uid, sub.plan, sub.status, **ids)}

    def billing_confirm(self, q, body):
        uid = self._uid(q, body)
        if not uid:
            return 401, {"error": "auth required"}
        pp = payments.get()
        fn = getattr(pp, "confirm", None)
        if not callable(fn):
            return 503, {"error": "payments not configured"}
        ref = (body or {}).get("reference") or q.get("reference") or ""
        sub = fn(ref)
        if not getattr(sub, "ok", False):
            return 402, {"error": "not confirmed",
                         "detail": getattr(sub, "detail", "")}
        # A session id is not a capability: without this check, anyone who
        # learned one could apply someone else's paid plan to their own account.
        if getattr(sub, "user", "") and sub.user != uid:
            return 403, {"error": "that checkout belongs to another account"}
        out = self._apply_subscription(uid, sub, ref, getattr(pp, "id", ""))
        resp = {"ok": True}
        resp.update(out)
        if "applied" in out:
            resp["subscription"] = billing.get(self.conn, uid)
        return 200, resp

    def billing_cancel(self, q, body):
        """Stop the next renewal. Access lasts to the end of the paid period.

        Without this a reader had no way to stop being billed except to ask us,
        and deleting the account did not cancel anything - the invoices kept
        coming to a customer who no longer existed. The provider is the authority:
        we ask it to stop, then record what it says.
        """
        uid = self._uid(q, body)
        if not uid:
            return 401, {"error": "auth required"}
        if not self._limit(self.buy_limiter, uid):
            return 429, {"error": "rate limited"}
        pp = payments.get()
        fn = getattr(pp, "cancel", None) if pp is not None else None
        if not callable(fn):
            return 503, {"error": "payments not configured"}
        sub = billing.get(self.conn, uid)
        sid = (sub or {}).get("subscription_id") or ""
        if not sid:
            return 409, {"error": "no subscription to cancel",
                         "detail": "this account has no recurring charge on file"}
        res = fn(sid)
        if not res.get("ok"):
            return 402, {"error": "cancel refused", "detail": res.get("detail", "")}
        if sub:
            billing.upsert(self.conn, uid, sub["plan"], sub["status"],
                           provider=sub["provider"], reference=sub["reference"],
                           current_period_end=res.get("current_period_end")
                           or sub["current_period_end"],
                           customer=sub.get("customer", ""),
                           subscription_id=sid)
        return 200, {"ok": True, "cancel_at_period_end":
                     bool(res.get("cancel_at_period_end")),
                     "current_period_end": res.get("current_period_end", 0),
                     "subscription": billing.get(self.conn, uid)}

    def billing_portal(self, q, body):
        """A provider-hosted page to change the card or the plan.

        Card details never reach this server, and a plan change is priced by the
        provider from its own catalog. Needs the customer id, which is why the
        checkout now stores it.
        """
        uid = self._uid(q, body)
        if not uid:
            return 401, {"error": "auth required"}
        if not self._limit(self.buy_limiter, uid):
            return 429, {"error": "rate limited"}
        pp = payments.get()
        fn = getattr(pp, "portal", None) if pp is not None else None
        if not callable(fn):
            return 503, {"error": "payments not configured"}
        sub = billing.get(self.conn, uid)
        customer = (sub or {}).get("customer") or ""
        if not customer:
            return 409, {"error": "no billing customer on file"}
        res = fn(customer, self._safe_return((body or {}).get("return_url"),
                                             (q or {}).get("_base", "")))
        if not res.get("ok"):
            return 402, {"error": "portal refused", "detail": res.get("detail", "")}
        return 200, {"ok": True, "url": res.get("url", "")}

    def _apply_subscription_state(self, st, provider_id):
        """Apply a lifecycle event to the row that names that subscription.

        Matched by the provider's subscription id, never by user: a renewal body
        carries no user id, and guessing one from a customer id would be worse
        than doing nothing. An event for a subscription we never stored is
        acknowledged and ignored.
        """
        sid = (st or {}).get("subscription_id", "")
        uid = billing.by_subscription(self.conn, sid)
        if not uid:
            return 200, {"ok": True, "applied": False,
                         "detail": "unknown subscription", "ignored": st.get("event", "")}
        status = st.get("status") or ""
        end = int(st.get("current_period_end") or 0)
        row = billing.get(self.conn, uid)
        if row and row.get("subscription_id") == sid:
            billing.upsert(self.conn, uid, row["plan"], status or row["status"],
                           provider=provider_id, reference=row["reference"],
                           current_period_end=end or row["current_period_end"],
                           customer=st.get("customer", ""), subscription_id=sid)
        addon = billing.addons(self.conn, uid).get(billing.STORAGE_ADDON)
        if addon and addon.get("subscription_id") == sid:
            billing.upsert_addon(self.conn, uid, billing.STORAGE_ADDON,
                                 addon["quantity"], status or addon["status"],
                                 provider=provider_id,
                                 reference=addon["reference"],
                                 current_period_end=end or addon["current_period_end"],
                                 customer=st.get("customer", ""),
                                 subscription_id=sid)
        return 200, {"ok": True, "applied": True, "user": uid,
                     "status": status, "event": st.get("event", "")}

    def payment_webhook(self, raw, sig_header, provider_id="stripe"):
        """Stripe's server-to-server view of a settled checkout.

        Public by necessity (Stripe cannot hold a session), so the signature is
        the whole authentication: nothing is read out of the body until the
        provider verifies it. On a completed checkout the session is re-read
        from Stripe and applied exactly as /billing/confirm applies it, so a
        buyer who closes the tab still gets their plan. Replays are harmless -
        the apply is an upsert and Stripe may retry.
        """
        pp = payments.get(provider_id)
        if pp is None:
            return 404, {"error": "unknown payment provider"}
        verify = getattr(pp, "verify_webhook", None)
        if not callable(verify):
            return 501, {"error": "provider has no webhook support"}
        event, err = verify(raw, sig_header)
        if event is None:
            return 400, {"error": "bad webhook", "detail": err}
        ref = ""
        rfn = getattr(pp, "webhook_reference", None)
        if callable(rfn):
            ref = rfn(event) or ""
        typ = (event or {}).get("type", "")
        obj = ((event or {}).get("data") or {}).get("object") or {}
        # A character purchase settles into a payment-mode session. Credit it
        # here so a buyer who closes the tab is not charged for nothing.
        if ref and (obj.get("metadata") or {}).get("kind") == "characters":
            pfn = getattr(pp, "purchase_confirm", None)
            if not callable(pfn):
                return 200, {"ok": True, "reference": ref, "applied": False,
                             "detail": "provider cannot confirm purchases"}
            p = pfn(ref)
            if not getattr(p, "ok", False):
                return 200, {"ok": True, "reference": ref, "applied": False,
                             "detail": getattr(p, "detail", "")}
            fresh = self._claim_payment(p.reference, "characters", p.user,
                                        p.reference, p.chars, "webhook")
            if fresh and p.chars and p.user:
                accounting.topup(self.conn, p.user, p.chars, currency="chars",
                                 note="purchase %d chars (%s)" % (p.chars, p.reference))
            return 200, {"ok": True, "reference": ref, "applied": fresh,
                         "user": p.user, "chars": p.chars}
        if not ref:
            # Renewals, failed cards, cancellations: no checkout session, but a
            # subscription id, which is the row we must update or the paid tier
            # never lapses.
            sfn = getattr(pp, "subscription_state", None)
            st = sfn(event) if callable(sfn) else None
            if st:
                return self._apply_subscription_state(st, getattr(pp, "id", ""))
            return 200, {"ok": True, "ignored": typ}
        sub = pp.confirm(ref)
        if not getattr(sub, "ok", False):
            # Not paid / not complete: acknowledge so Stripe stops retrying, but
            # record nothing. Money that has not settled is not a sale.
            return 200, {"ok": True, "reference": ref, "applied": False,
                         "detail": getattr(sub, "detail", "")}
        uid = getattr(sub, "user", "") or ""
        if not uid:
            return 200, {"ok": True, "reference": ref, "applied": False,
                         "detail": "no user on checkout session"}
        self._apply_subscription(uid, sub, ref, getattr(pp, "id", ""))
        return 200, {"ok": True, "reference": ref, "applied": True, "user": uid}


    def job(self, q, body):
        j = jobs.job(self.conn, q.get("id", ""))
        return (200, j) if j else (404, {"error": "no job"})

    def jobs_list(self, q, body):
        """This user's recent conversions, newest first, so the client can show
        a queued or running one and let the reader cancel it after a reload."""
        uid = self._uid(q, body)
        return 200, {"jobs": jobs.list_for(self.conn, uid, limit=20)}

    def cancel_job(self, q, body):
        """Stop a queued or running render. The row is marked at once so the
        page sees it, and the worker is signalled so it unwinds before its next
        chapter. A job that already finished is reported, not restarted."""
        uid = self._uid(q, body)
        jid = body.get("job") or q.get("job") or q.get("id") or ""
        j = jobs.job(self.conn, jid)
        if not j:
            return 404, {"error": "no job"}
        if j["user_id"] != uid and not auth.is_admin(self.conn, uid):
            return 403, {"error": "not your job"}
        if j["status"] in ("done", "error", "cancelled"):
            return 200, {"cancel": False, "job": j, "reason": j["status"]}
        jobs.request_cancel(jid)
        self.conn.execute(
            "UPDATE jobs SET status='cancelled',error=?,updated_at=? WHERE id=?",
            ("cancelled by user", db.now(), jid))
        self.conn.commit()
        return 200, {"cancel": True, "job": jobs.job(self.conn, jid)}

    def estimate(self, q, body):
        """Pre-convert time and disk, before the user commits to a long job."""
        book_id = q.get("book", "")
        provider = q.get("provider", "")
        rows = library.chapters(self.conn, book_id)
        idxs = [c["idx"] for c in rows]
        only = q.get("chapters")
        if only:
            sel = set()
            for x in str(only).split(","):
                x = x.strip()
                if x:
                    try:
                        sel.add(int(x))
                    except ValueError:
                        pass
            idxs = [i for i in idxs if i in sel]
        chars = sum((c["chars"] or 0) for c in rows if c["idx"] in set(idxs))
        return 200, jobs.estimate(chars, provider)

    def progress(self, q, body):
        book_id = q.get("book", "")
        b = library.get_book(self.conn, book_id) or {}
        ch = library.chapters(self.conn, book_id)
        rends = {}
        for r in library.renditions(self.conn, book_id):
            rends.setdefault(r["idx"], []).append({
                "provider": r["provider"], "voice": r["voice"],
                "voice_label": r["voice_label"], "model": r["model"],
                "audio_ref": r["audio_ref"], "audio_bytes": r["audio_bytes"]})
        return 200, {"book": self._book_payload(book_id),
                     "chapters": [
            {"idx": c["idx"], "title": c["title"], "chars": c["chars"],
             "rendered": bool(c["audio_ref"]),
             "audio_ref": c["audio_ref"], "audio_bytes": c["audio_bytes"],
             "provider": c["provider"], "voice": c["voice"], "model": c["model"],
             "renditions": rends.get(c["idx"], [])}
            for c in ch]}

    def text(self, q, body):
        """The chapter's text, for the now-playing page. Not the audio path."""
        try:
            idx = int(q["idx"])
            t = library.chapter_text(self.conn, self.storage, q["book"], idx)
        except (KeyError, ValueError):
            return 404, {"error": "no chapter"}
        rows = [c for c in library.chapters(self.conn, q["book"]) if c["idx"] == idx]
        # Reading blocks: the same text cut into sentences, each weighted by how
        # long the voice takes to say it, so the player can mark the one it is
        # speaking. Weights use the pronounced text, because that is what was
        # recorded. A client that does not know this field renders `text`.
        uid = self._uid(q, body)
        rules = pron.load(self.conn, uid) if uid else []
        blocks = reading.sentences(t, weigh=lambda s: len(pron.spoken(s, rules)))
        return 200, {"text": t, "title": rows[0]["title"] if rows else "",
                     "sentences": blocks}

    def chapter(self, q, body):
        idx = int(q["idx"])
        book_id = q["book"]
        voice = q.get("voice") or ""
        provider = q.get("provider") or ""
        model = q.get("model") or ""
        speed = q.get("speed") or ""
        if voice or provider:
            # a named voice: serve that rendition, and say so if it is not there
            r = library.rendition(self.conn, book_id, idx, provider, voice, model,
                                  speed=speed)
            if not r:
                return 409, {"error": "not rendered in that voice"}
            data = self.storage.get(r["audio_ref"])
            return 200, {"_raw": data, "_mime": "audio/mpeg", "bytes": len(data),
                         "checksum": r["audio_ref"]}
        rows = [r for r in library.chapters(self.conn, book_id) if r["idx"] == idx]
        if not rows:
            return 404, {"error": "no chapter"}
        r = rows[0]
        if not r["audio_ref"]:
            return 409, {"error": "not rendered"}
        data = self.storage.get(r["audio_ref"])
        return 200, {"_raw": data, "_mime": "audio/mpeg", "bytes": len(data),
                     "checksum": r["audio_ref"]}

    # ---- voice sandbox --------------------------------------------------------
    # Say a short bit of free text in one voice, right now, and hand the audio
    # straight back. Not a render: nothing is stored, nothing is metered, and the
    # only limit is the provider's own. The sandbox page drives this once per
    # voice for the scan button.

    SPEAK_MAX_CHARS = 1000

    def speak(self, q, body):
        uid = self._uid(q, body)
        text = (body.get("text") or "").strip()
        if not text:
            return 400, {"error": "nothing to say"}
        if len(text) > self.SPEAK_MAX_CHARS:
            return 400, {"error": "text is longer than %d characters" % self.SPEAK_MAX_CHARS}
        cid = body.get("connection_id") or ""
        provider = body.get("provider", "")
        cred = None
        if cid:
            c = connections.get(self.conn, cid)
            if not connections.may_use(c, uid, auth.is_admin(self.conn, uid)):
                return 403, {"error": "no such connection"}
            cred = connections.resolve(self.conn, uid, cid)
            if cred is None:
                return 403, {"error": "connection is switched off"}
            provider = c["provider"]
        if provider not in self.host.speech:
            return 400, {"error": "unknown provider"}
        if cred is None:
            cred = body.get("cred") or credentials.get_credential(self.conn, uid, provider)
        voice = body.get("voice", "")
        model = body.get("model", "")
        speed = float(body.get("speed", 1.0) or 1.0)
        if self.rate_limiter is not None and not self.rate_limiter.allow(uid):
            return 429, {"error": "rate limited"}
        speech = self.host.speech[provider].obj
        audio = speech.synth(text, voice, {"model": model, "speed": speed}, cred)
        data = getattr(audio, "data", None) or getattr(audio, "bytes", None) or b""
        if not data:
            return 502, {"error": "the voice server returned no audio"}
        return 200, {"_raw": data, "_mime": getattr(audio, "mime", "audio/mpeg"),
                     "bytes": len(data), "chars": len(text)}

    # ---- playback -------------------------------------------------------------

    def get_position(self, q, body):
        return 200, playback.get_position(self.conn, self._uid(q, None), q["book"]) or {}

    def last_position(self, q, body):
        return 200, playback.last_position(self.conn, self._uid(q, None))

    def put_position(self, q, body):
        return 200, playback.put_position(self.conn, self._uid(q, body), body["book"],
                                          body["chapter_index"], body["offset_seconds"],
                                          body.get("device", ""))

    def claim(self, q, body):
        return 200, playback.claim(self.conn, self._uid(q, body), body["book"], body["device"])

    def stop(self, q, body):
        return 200, {"stop": playback.stop_pending(self.conn, self._uid(q, None), q["book"],
                                                   q["device"])}

    def ack(self, q, body):
        playback.ack_stop(self.conn, self._uid(q, body), body["book"], body["device"])
        return 200, {"ok": True}

    def stats_get(self, q, body):
        return 200, stats.user_stats(self.conn, self._uid(q, None))

    def listen(self, q, body):
        stats.add_listen(self.conn, self._uid(q, body), body.get("book", ""),
                         body.get("seconds", 0))
        return 200, {"ok": True}

    # ---- credentials (hosted; values never leave) -----------------------------

    def put_credential(self, q, body):
        credentials.set_credential(self.conn, self._uid(q, body), body["provider"],
                                   body.get("data") or {})
        return 200, {"ok": True, "providers": credentials.providers(self.conn, self._uid(q, body))}

    # ---- source credentials (server-side, never returned) ---------------------

    def _source_field_defs(self, sid):
        for p in self.host.readers:
            if p.id == sid:
                return self._fields(p.obj)
        return []

    def source_creds(self, q, body):
        """Every reader that declares settings, with its fields and whether the
        signed-in user has a stored value. A value is never returned."""
        uid = self._uid(q, body)
        out = []
        for p in self.host.readers:
            fields = self._fields(p.obj)
            if not fields:
                continue
            stored = credentials.get_credential(self.conn, uid, "source:" + p.id)
            fs = []
            for f in fields:
                name = f.get("name") or f.get("key") or ""
                kind = f.get("kind") or f.get("type") or "text"
                sensitive = self._sensitive(name, kind)
                fs.append({"name": name, "label": f.get("label") or name,
                           "kind": kind, "help": f.get("help") or "",
                           "placeholder": f.get("placeholder") or "",
                           "sensitive": sensitive,
                           "set": bool(stored.get(name)) if stored else False,
                           "value": ""})
            out.append({"id": p.id, "label": p.label, "fields": fs})
        return 200, {"sources": out}

    def source_cred_save(self, q, body):
        """Store a source's credential server-side for the signed-in user. An
        empty field leaves the stored value alone, so re-saving a form that does
        not re-type a secret never blanks it."""
        uid = self._uid(q, body)
        sid = body.get("id") or ""
        defs = {f.get("name") or f.get("key"): (f.get("kind") or f.get("type") or "text")
                for f in self._source_field_defs(sid)}
        if not defs:
            return 400, {"error": "unknown source"}
        cur = credentials.get_credential(self.conn, uid, "source:" + sid)
        for k, v in (body.get("data") or {}).items():
            if k in defs and v not in (None, ""):
                cur[k] = v
        credentials.set_credential(self.conn, uid, "source:" + sid, cur)
        return 200, {"ok": True}

    def source_cred_delete(self, q, body):
        uid = self._uid(q, body)
        credentials.delete_credential(self.conn, uid, "source:" + (body.get("id") or ""))
        return 200, {"ok": True}

    # ---- connections (voice servers, kept on the server) ---------------------

    def _field_defs(self, provider):
        p = self.host.speech.get(provider)
        return self._fields(p.obj) if p else []

    def _sensitive(self, name, kind):
        return kind == "password" or name in ("key", "token", "cookie", "password", "secret")

    def _conn_view(self, c, uid, is_admin):
        """A connection as the client may see it: never a secret value."""
        fields = []
        for f in self._field_defs(c["provider"]):
            name = f.get("name") or f.get("key") or ""
            kind = f.get("kind") or f.get("type") or "text"
            merged = dict(c["data"]); merged.update(c.get("override") or {})
            val = merged.get(name)
            sensitive = self._sensitive(name, kind)
            fields.append({"name": name, "label": f.get("label") or name, "kind": kind,
                           "sensitive": sensitive, "set": val not in (None, ""),
                           "value": "" if sensitive else (val or "")})
        return {"id": c["id"], "scope": c["scope"], "provider": c["provider"],
                "label": c["label"], "mine": c["mine"], "system": c["system"],
                "enabled": c["enabled"],
                "editable": connections.may_edit(c, uid, is_admin), "fields": fields}

    def connections_list(self, q, body):
        uid = self._uid(q, body)
        is_admin = auth.is_admin(self.conn, uid)
        out = [self._conn_view(c, uid, is_admin)
               for c in connections.list_for(self.conn, uid)]
        prefs = connections.get_prefs(self.conn, uid)
        return 200, {"connections": out, "admin": is_admin,
                     "default_connection_id": prefs["default_connection_id"],
                     "default_voice": prefs["default_voice"],
                     "reading": prefs["reading"]}

    def _writable_fields(self, provider, uid, is_admin):
        """The field-name -> kind map for a provider (who may write what is
        decided by the caller)."""
        return {f.get("name") or f.get("key"): (f.get("kind") or f.get("type") or "text")
                for f in self._field_defs(provider)}

    def connection_save(self, q, body):
        uid = self._uid(q, body)
        is_admin = auth.is_admin(self.conn, uid)
        cid = body.get("id") or None
        scope = body.get("scope") or connections.PERSONAL
        provider = body.get("provider") or ""
        if cid:
            cur = connections.get(self.conn, cid)
            if not connections.may_edit(cur, uid, is_admin):
                return 403, {"error": "not yours to edit"}
            provider, scope = provider or cur["provider"], cur["scope"]
            # an admin may share a server with the household, or stop sharing it
            if is_admin and body.get("scope") in (connections.SYSTEM, connections.PERSONAL):
                scope = body["scope"]
        elif scope == connections.SYSTEM and not is_admin:
            return 403, {"error": "only an admin adds a shared server"}
        if provider not in self.host.speech:
            return 400, {"error": "unknown provider"}
        defs = self._writable_fields(provider, uid, is_admin)
        data = {k: v for k, v in (body.get("data") or {}).items()
                if k in defs and (is_admin or not self._sensitive(k, defs[k]))}
        cid = connections.upsert(self.conn, uid, provider, body.get("label"),
                                 data, scope=scope, conn_id=cid)
        return 200, {"ok": True, "id": cid}

    def connection_delete(self, q, body):
        uid = self._uid(q, body)
        cur = connections.get(self.conn, body.get("id") or "")
        if not connections.may_edit(cur, uid, auth.is_admin(self.conn, uid)):
            return 403, {"error": "not yours to remove"}
        connections.delete(self.conn, cur["id"])
        return 200, {"ok": True}

    def connection_override(self, q, body):
        """A user's own switch and non-secret edits for one connection."""
        uid = self._uid(q, body)
        if not uid:
            return 401, {"error": "auth required"}
        cur = connections.get(self.conn, body.get("id") or "")
        if not connections.may_use(cur, uid, auth.is_admin(self.conn, uid)):
            return 403, {"error": "no such connection"}
        defs = self._writable_fields(cur["provider"], uid, False)
        data = {k: v for k, v in (body.get("data") or {}).items()
                if k in defs and not self._sensitive(k, defs[k])}
        connections.set_override(self.conn, uid, cur["id"],
                                 enabled=body.get("enabled"), data=data)
        return 200, {"ok": True}

    def prefs_set(self, q, body):
        uid = self._uid(q, body)
        if not uid:
            return 401, {"error": "auth required"}
        kw = {}
        if "default_connection_id" in body:
            kw["connection_id"] = body.get("default_connection_id") or ""
        if "default_voice" in body:
            kw["voice"] = body.get("default_voice") or ""
        if "reading" in body and isinstance(body.get("reading"), dict):
            kw["reading"] = body.get("reading")
        connections.set_prefs(self.conn, uid, **kw)
        return 200, {"ok": True}

    # ---- pronunciation fixes (per user, applied before any engine) ----------

    def pron_get(self, q, body):
        uid = self._uid(q, body)
        return 200, {"rules": pron.load(self.conn, uid)}

    def pron_set(self, q, body):
        uid = self._uid(q, body)
        if not uid:
            return 401, {"error": "auth required"}
        return 200, {"ok": True,
                     "rules": pron.save(self.conn, uid, body.get("rules"))}

    def pron_impact(self, q, body):
        """Which already-voiced chapters a rule change makes out of date.

        Read-only: it names the work (per book, voice and chapter) so the UI can
        offer to re-encode just that, instead of a whole-book convert.
        """
        uid = self._uid(q, body)
        if not uid:
            return 401, {"error": "auth required"}
        return 200, pron.impact(self.conn, self.storage, uid,
                                q.get("book") or None,
                                split_for=self._speech_split)

    def _speech_split(self, provider):
        """A provider's chunk function, or None. Lets the impact report measure
        the truly new audio instead of assuming a whole chapter re-renders."""
        p = self.host.speech.get(provider)
        return getattr(p.obj, "split", None) if p else None

    # ---- auth (social sign-in; the session lives in sqlite) ------------------

    def auth_providers(self, q, body):
        return 200, {"providers": self.auth.providers()}

    def auth_policy(self, q, body):
        """What a stranger may do before they have a session."""
        return 200, {
            "providers": self.auth.providers(),
            "has_accounts": auth.account_count(self.conn) > 0,
            "registration_open": auth.registration_open(self.conn),
        }

    def auth_users(self, q, body):
        uid = (q or {}).get("_user") or ""
        if not auth.is_admin(self.conn, uid):
            return 403, {"error": "admin only"}
        return 200, {"users": auth.local_accounts(self.conn),
                     "registration_open": auth.registration_open(self.conn),
                     "grants": grants.list_all(self.conn),
                     "you": uid}

    def grants_list(self, q, body):
        """Admin: every account and its lifted-limit grants."""
        uid = (q or {}).get("_user") or ""
        if not auth.is_admin(self.conn, uid):
            return 403, {"error": "admin only"}
        return 200, {"users": auth.local_accounts(self.conn),
                     "grants": grants.list_all(self.conn)}

    def grants_set(self, q, body):
        """Admin: lift or restore the storage and/or credit check for a user.
        The account's own usage and charges are untouched."""
        uid = (q or {}).get("_user") or ""
        if not auth.is_admin(self.conn, uid):
            return 403, {"error": "admin only"}
        target = (body or {}).get("user") or ""
        if not target:
            return 400, {"error": "user required"}
        body = body or {}
        out = grants.set_grants(
            self.conn, target,
            storage=(bool(body.get("storage_unlimited"))
                     if "storage_unlimited" in body else None),
            credits=(bool(body.get("credits_unlimited"))
                     if "credits_unlimited" in body else None))
        return 200, {"ok": True, "user": target, "grants": out}

    # ---- plans (admin) -------------------------------------------------------
    # An admin sets what tier an account is on without a payment. This is how a
    # comped account (family, a tester) gets the paid tier; usage and the
    # allowance are the tier's own, so the screen stays honest.

    def plans_admin(self, q, body):
        """Admin: every account, its plan, and its storage blocks."""
        uid = (q or {}).get("_user") or ""
        if not auth.is_admin(self.conn, uid):
            return 403, {"error": "admin only"}
        subs = billing.list_all(self.conn)
        admins = {r["user_id"] for r in
                  self.conn.execute("SELECT user_id FROM admins")}
        rows = []
        for r in self.conn.execute("SELECT id,name FROM users ORDER BY created_at"):
            uid_row = r["id"]
            sub = subs.get(uid_row)
            rows.append({
                "username": r["name"] or uid_row,
                "user_id": uid_row,
                "admin": uid_row in admins,
                "plan": (sub or {}).get("plan") or "",
                "plan_status": (sub or {}).get("status") or "",
                "plan_provider": (sub or {}).get("provider") or "",
                "storage_blocks": billing.blocks(self.conn, uid_row),
            })
        return 200, {"users": rows,
                     "tiers": [quota.FREE_PLAN, quota.PLATFORM_PLAN]}

    def plans_admin_set(self, q, body):
        """Admin: set one account's tier (free or platform). Records it like a
        provider would, with `admin` as the provider, so nothing downstream can
        tell a comp from a paid row except the provider field."""
        uid = (q or {}).get("_user") or ""
        if not auth.is_admin(self.conn, uid):
            return 403, {"error": "admin only"}
        body = body or {}
        target = (body.get("user") or "").strip()
        plan = (body.get("plan") or "").strip().lower()
        tiers = (quota.FREE_PLAN, quota.PLATFORM_PLAN)
        if not target:
            return 400, {"error": "user required"}
        if plan not in tiers:
            return 400, {"error": "plan must be one of %s" % (tiers,)}
        row = billing.upsert(self.conn, target, plan, "active", provider="admin")
        return 200, {"ok": True, "subscription": row}

    def set_registration(self, q, body):
        uid = (q or {}).get("_user") or ""
        if not auth.is_admin(self.conn, uid):
            return 403, {"error": "admin only"}
        v = auth.set_registration(self.conn, bool((body or {}).get("open")))
        return 200, {"ok": True, "registration": v}

    def me(self, q, body):
        uid = (q or {}).get("_user") or ""
        return 200, {"user": uid, "authenticated": bool(uid),
                     "admin": auth.is_admin(self.conn, uid),
                     # about yourself only: the account panel reads this to ask
                     # for a password (local) or a typed phrase (social-only).
                     "has_password": bool(uid) and auth.has_local_account(self.conn, uid)}

    def auth_linked(self, q, body):
        """Which social providers the signed-in user has attached."""
        uid = (q or {}).get("_user") or ""
        if not uid:
            return 401, {"error": "auth required"}
        return 200, {"linked": auth.linked_providers(self.conn, uid)}

    # ---- devices and sessions (see and end a session anywhere) ---------------

    def _session_state(self, q, uid):
        """The caller's own sessions, plus (for an admin) every session on the
        server, so the same panel can boot a device anywhere."""
        tok = (q or {}).get("_token") or ""
        out = {"sessions": auth.list_sessions(self.conn, uid, tok)}
        if auth.is_admin(self.conn, uid):
            out["admin"] = True
            out["all"] = auth.list_all_sessions(self.conn, tok)
        return out

    def sessions_list(self, q, body):
        """Every live session of the signed-in user, the current one flagged.
        An admin also gets every session on the server."""
        uid = (q or {}).get("_user") or ""
        if not uid:
            return 401, {"error": "auth required"}
        return 200, self._session_state(q, uid)

    def session_revoke(self, q, body):
        """Log out one session by its id. A user may end any of their own; an
        admin may end any session on the server. A stale id is not found."""
        uid = (q or {}).get("_user") or ""
        if not uid:
            return 401, {"error": "auth required"}
        sid = ((body or {}).get("id") or "").strip()
        ok = auth.revoke_session(self.conn, uid, sid)
        if not ok and auth.is_admin(self.conn, uid):
            ok = auth.revoke_any(self.conn, sid) is not None
        out = self._session_state(q, uid)
        out["ok"] = ok
        return (200 if ok else 404), out

    def sessions_revoke_all(self, q, body):
        """Log out: the caller's own sessions (default), or, for an admin with
        {all_users: true}, every session on the server. Either way keep the one
        calling, so the button does not sign you out of the page you are on."""
        uid = (q or {}).get("_user") or ""
        if not uid:
            return 401, {"error": "auth required"}
        others = (body or {}).get("others")
        keep_current = others is None or _truthy(others)
        keep = ((q or {}).get("_token") or "") if keep_current else ""
        if _truthy((body or {}).get("all_users")):
            if not auth.is_admin(self.conn, uid):
                return 403, {"error": "admin only"}
            n = auth.revoke_all_sessions(self.conn, keep)
        else:
            n = auth.revoke_sessions(self.conn, uid, keep)
        out = self._session_state(q, uid)
        out["ok"] = True
        out["revoked"] = n
        return 200, out

    # ---- erasing the account --------------------------------------------------

    # ---- the account's own password --------------------------------------------

    def account_password(self, q, body):
        """Set or change the signed-in account's password.

        A password account must give the current one. A social-only account has
        none, so it names itself a sign-in name and gets a password. Either way
        every OTHER session is revoked: a password change should end sessions
        won under the old one, while the device doing the changing stays in.
        """
        uid = (q or {}).get("_user") or ""
        if not uid:
            return 401, {"error": "auth required"}
        body = body or {}
        try:
            name = auth.set_password(self.conn, uid,
                                     body.get("current") or "",
                                     body.get("password") or "",
                                     body.get("username") or "")
        except auth.AuthError as e:
            return 400, {"error": str(e)}
        ended = auth.revoke_sessions(self.conn, uid, (q or {}).get("_token") or "")
        return 200, {"ok": True, "username": name, "has_password": True,
                     "ended": ended}

    def _cancel_billing_for(self, uid):
        """Stop the recurring charge before the account is erased.

        Deleting the rows on our side never reached the provider, so a deleted
        account kept being invoiced. Asking first is the whole fix; a failure is
        reported rather than hidden, because an invoice arriving for a customer
        who no longer exists is exactly the kind of thing the buyer must not
        have to discover alone.
        """
        sub = billing.get(self.conn, uid) or {}
        sid = sub.get("subscription_id") or ""
        if not sid:
            return {"cancelled": False, "detail": "no subscription on file"}
        pp = payments.get(sub.get("provider") or None)
        fn = getattr(pp, "cancel", None) if pp is not None else None
        if not callable(fn):
            return {"cancelled": False, "detail": "payments not configured"}
        res = fn(sid)
        return {"cancelled": bool(res.get("ok")),
                "subscription_id": sid,
                "detail": res.get("detail", "")}

    def account_delete(self, q, body):
        """Erase the signed-in account for good: books, voiced audio, history,
        credentials, and sign-ins.

        Proof first, the way the account already signs in: a password account
        proves with its password; a social-only account has no password to give,
        so it must type the confirmation phrase. The last admin cannot delete
        itself - that would leave the server with nobody who can run it."""
        uid = (q or {}).get("_user") or ""
        if not uid:
            return 401, {"error": "auth required"}
        body = body or {}
        if auth.is_admin(self.conn, uid) and auth.admin_count(self.conn) <= 1:
            return 409, {"error": "last admin",
                         "detail": "this is the only admin account. Make another "
                                   "account an admin and delete this one after."}
        if auth.has_local_account(self.conn, uid):
            if not auth.local_password_proof(self.conn, uid,
                                             body.get("password") or ""):
                return 403, {"error": "wrong password"}
        elif (body.get("confirm") or "").strip().lower() != "delete my account":
            return 400, {"error": "confirm",
                         "detail": "type 'delete my account' to confirm"}
        billing_out = self._cancel_billing_for(uid)
        out = erase.purge_user(self.conn, self.storage, uid)
        if billing_out.get("subscription_id") and not billing_out["cancelled"]:
            # keep a trail an operator can finish by hand, since the rows that
            # named the subscription are gone with the account
            self._claim_payment("cancel-failed:%s:%s" % (uid, db.now()),
                                "cancel_failed", uid,
                                billing_out.get("subscription_id", ""), 0,
                                billing_out.get("detail", ""))
        out["billing"] = billing_out
        out["ok"] = True
        out["logout"] = True
        return 200, out


GET_ROUTES = {
    "/": "ui", "/index.html": "ui", "/favicon.ico": "favicon", "/i18n.js": "i18n_js",
    "/manifest.webmanifest": "manifest", "/sw.js": "service_worker",
    "/icon-192.png": "icon192", "/icon-512.png": "icon512",
    "/icon-maskable-512.png": "icon_maskable",
    "/privacy": "privacy", "/terms": "terms", "/delete-account": "delete_account",
    "/health": "health", "/plugins": "plugins", "/library": "library_list",
    "/series": "series_list",
    "/job": "job", "/jobs": "jobs_list", "/progress": "progress", "/chapter": "chapter",
    "/estimate": "estimate",
    "/position": "get_position", "/last": "last_position", "/stop": "stop", "/text": "text",
    "/stats": "stats_get", "/pron": "pron_get",
    "/pron/impact": "pron_impact",
    "/connections": "connections_list", "/source-creds": "source_creds",
    "/cover": "cover_get",
    "/auth/providers": "auth_providers", "/me": "me", "/auth/policy": "auth_policy",
    "/auth/linked": "auth_linked",
    "/sessions": "sessions_list",
    "/auth/users": "auth_users", "/credits": "credits",
    "/grants": "grants_list",
    "/admin/plans": "plans_admin",
    "/billing": "billing_get",
    "/plans": "plans_get",
    "/storage": "storage_get",
}
POST_ROUTES = {
    "/import": "import_book", "/import/preview": "preview_import", "/book/example": "add_example_book",
    "/render": "render", "/voices": "voices",
    "/book/renditions/delete": "remove_renditions", "/book/delete": "delete_book",
    "/account/delete": "account_delete",
    "/account/password": "account_password",
    "/speak": "speak",
    "/cover": "put_cover", "/cover/auto": "cover_auto", "/cover/clear": "clear_cover",
    "/book/details": "fetch_details", "/book/meta": "book_meta_set",
    "/book/series": "book_series_set",
    "/series": "series_save", "/series/move": "series_move",
    "/position": "put_position", "/claim": "claim", "/ack": "ack",
    "/listen": "listen", "/cancel": "cancel_job",
    "/credential": "put_credential", "/auth/registration": "set_registration",
    "/connections": "connection_save", "/connections/delete": "connection_delete",
    "/source-creds": "source_cred_save", "/source-creds/delete": "source_cred_delete",
    "/connections/override": "connection_override", "/prefs": "prefs_set",
    "/pron": "pron_set", "/credits/purchase": "credits_purchase",
    "/grants": "grants_set",
    "/admin/plan": "plans_admin_set",
    "/billing/checkout": "billing_checkout", "/billing/confirm": "billing_confirm",
    "/billing/cancel": "billing_cancel", "/billing/portal": "billing_portal",
    "/credits/confirm": "credits_confirm",
    "/sessions/revoke": "session_revoke",
    "/sessions/revoke_all": "sessions_revoke_all",
}

# reachable without a session even when auth is required: the page, its health,
# the sign-in surface, and the browser's cache of who it is.
PUBLIC_ROUTES = {"/", "/index.html", "/i18n.js", "/favicon.ico", "/health", "/auth/providers",
                 "/auth/policy", "/auth/forgot", "/auth/reset", "/me", "/plans",
                 "/privacy", "/terms", "/delete-account",
                 "/manifest.webmanifest", "/sw.js",
                 "/icon-192.png", "/icon-512.png", "/icon-maskable-512.png"}

# auth sub-routes the handler serves directly (they set cookies / redirect),
# so they are not in the GET/POST dispatch tables above. Declared here so the
# browser client's served-route guard can see them too.
AUTH_ROUTES = {"/auth/logout", "/auth/claim", "/auth/claim/confirm",
               "/auth/local/register", "/auth/local/login",
               "/auth/forgot", "/auth/reset"}


def _with_idem(fn, *args, **kwargs):
    """Call a provider method, passing `idem` only if it takes one.

    Third-party providers (and the fakes the tests drive the host with) predate
    idempotency, so a missing keyword is not an error - it just means that
    provider cannot dedupe at its end and the host's own ledger has to.
    """
    idem = kwargs.pop("idem", "")
    if idem:
        try:
            if "idem" in inspect.signature(fn).parameters:
                kwargs["idem"] = idem
        except (TypeError, ValueError):
            pass
    return fn(*args, **kwargs)


def _truthy(v):
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "fireside-host/2"

        # ---- cookies + redirects ---------------------------------------------
        def _cookie(self, name):
            raw = self.headers.get("Cookie", "")
            for part in raw.split(";"):
                k, _, v = part.strip().partition("=")
                if k == name:
                    return v
            return ""

        def _base_url(self):
            """Where this box is reachable from the caller's side.

            Behind the Azure ingress the connection is http and the browser is
            not, so the scheme has to come from the proxy header - otherwise
            every link we mint (a password reset, a Stripe return) sends the
            browser to http:// and the page has to redirect to get back to https.
            """
            env = os.environ.get("FIRESIDE_BASE_URL")
            if env:
                return env.rstrip("/")
            proto = (self.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip()
            if proto not in ("http", "https"):
                proto = "http"
            return proto + "://" + (self.headers.get("Host") or "127.0.0.1:8788")

        def _cookie_str(self, name, value, path="/", max_age=None):
            c = "%s=%s; Path=%s; HttpOnly; SameSite=Lax" % (name, value, path)
            if max_age is not None:
                c += "; Max-Age=%d" % max_age
            if self._base_url().startswith("https"):
                c += "; Secure"
            return c

        def _redirect(self, location, cookies=None):
            self.send_response(302)
            self.send_header("Location", location)
            for c in (cookies or []):
                self.send_header("Set-Cookie", c)
            self.end_headers()

        def _token(self):
            tok = self._cookie(auth.SESSION_COOKIE)
            if not tok:
                authz = self.headers.get("Authorization", "")
                if authz.startswith("Bearer "):
                    tok = authz[7:]
            return tok

        def _ua(self):
            return (self.headers.get("User-Agent") or "")[:400]

        def _ip(self):
            # behind the tunnel the peer address is the proxy; trust only the
            # socket peer, and say so plainly rather than trust an XFF header.
            return (self.client_address[0] if self.client_address else "")[:64]

        def _auth_route(self, parts, parsed):
            from urllib.parse import parse_qs
            q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            pid, action = parts[1], parts[2]
            if pid not in app.auth.table:
                return self.send_error(404)
            try:
                redirect_uri = self._base_url() + "/auth/%s/callback" % pid
                if action == "start":
                    link = _truthy(q.get("link"))
                    # ?app=1 means the browser tab was opened by the Android
                    # shell, so the finished session has to be handed back.
                    for_app = _truthy(q.get("app"))
                    claim = q.get("claim") or ""
                    uid = auth.session_user(app.conn, self._token()) or ""
                    if link and not uid:
                        return self.send_error(401, "sign in before linking an account")
                    if claim:
                        auth.take_claim(app.conn, claim)   # expired/unknown -> AuthError
                    state = auth.new_state()
                    # PKCE providers (X) need a verifier kept for the callback.
                    verifier, _challenge = auth.new_pkce()
                    url = app.auth.authorize_url(pid, redirect_uri, state, verifier)
                    return self._redirect(url, [
                        self._cookie_str(auth.STATE_COOKIE, state, path="/auth", max_age=600),
                        self._cookie_str(auth.LINK_COOKIE, uid if link else "",
                                         path="/auth", max_age=600),
                        self._cookie_str(auth.CLAIM_COOKIE, claim,
                                         path="/auth", max_age=600),
                        self._cookie_str(auth.APP_COOKIE, "1" if for_app else "",
                                         path="/auth", max_age=600),
                        self._cookie_str(auth.PKCE_COOKIE, verifier,
                                         path="/auth", max_age=600)])
                if action == "callback":
                    if q.get("error"):
                        return self.send_error(400, "oauth error: %s" % q["error"])
                    if not q.get("state") or q["state"] != self._cookie(auth.STATE_COOKIE):
                        return self.send_error(400, "bad oauth state")
                    for_app = bool(self._cookie(auth.APP_COOKIE))
                    verifier = self._cookie(auth.PKCE_COOKIE)
                    clear = [self._cookie_str(auth.STATE_COOKIE, "", path="/auth", max_age=0),
                             self._cookie_str(auth.LINK_COOKIE, "", path="/auth", max_age=0),
                             self._cookie_str(auth.CLAIM_COOKIE, "", path="/auth", max_age=0),
                             self._cookie_str(auth.APP_COOKIE, "", path="/auth", max_age=0),
                             self._cookie_str(auth.PKCE_COOKIE, "", path="/auth", max_age=0)]
                    link_user = self._cookie(auth.LINK_COOKIE)
                    claim_tok = self._cookie(auth.CLAIM_COOKIE)
                    if link_user:
                        # a link flow, not a login: it must resolve to the same
                        # session that started it, and it never mints a session.
                        cur = auth.session_user(app.conn, self._token()) or ""
                        if cur != link_user:
                            return self.send_error(401, "session changed during link")
                        app.auth.link(pid, q.get("code", ""), redirect_uri, link_user,
                                      verifier)
                        return self._redirect("/?linked=%s" % pid, clear)
                    if claim_tok:
                        # proving ownership of an account a social login collided
                        # with: signing in through one of ITS OWN providers is the
                        # proof. Wrong provider / not this account -> do nothing.
                        try:
                            ident = app.auth.identity_from_code(pid, q.get("code", ""),
                                                                redirect_uri, verifier)
                            uid = auth.complete_claim_via_identity(app.conn, claim_tok,
                                                                   pid, ident)
                        except auth.AuthError as e:
                            return self._redirect(
                                "/?claim=%s&claimerr=%s" % (
                                    urllib.parse.quote(claim_tok),
                                    urllib.parse.quote(str(e))), clear)
                        token = auth.create_session(app.conn, uid, "link",
                                                    ua=self._ua(), ip=self._ip())
                        return self._redirect("/", clear + [self._cookie_str(
                            auth.SESSION_COOKIE, token, max_age=auth.SESSION_TTL)])
                    uid, token = app.auth.login(pid, q.get("code", ""), redirect_uri,
                                                ua=self._ua(), ip=self._ip(),
                                                verifier=verifier)
                    if for_app:
                        # The tab cannot set the webview's cookie. Hand back a
                        # one-time token and let /auth/app/session finish the
                        # job inside the app.
                        hand = auth.mint_app_handoff(app.conn, uid)
                        return self._redirect(
                            "/auth/app/done?token=" + urllib.parse.quote(hand), clear)
                    return self._redirect("/", clear + [self._cookie_str(
                        auth.SESSION_COOKIE, token, max_age=auth.SESSION_TTL)])
            except auth.AccountExistsError as e:
                # The provider proved the mailbox, not the account. Do not drop
                # them into it; hold the identity and ask for the account's own
                # login. Never mint a session on a collision.
                claim = auth.create_claim(app.conn, e.provider, e.ident, e.user_id)
                return self._redirect("/?claim=%s" % claim, clear)
            except auth.AuthError as e:
                return self._json(401, {"error": str(e)})
            return self.send_error(404)

        def _app_auth_route(self, path, u):
            """The Android app's two ends of a browser-tab sign-in.

            /auth/app/done runs in the browser tab (or Custom Tab): it offers the
            one tap that hands the token back over the fireside:// scheme, and
            tries it on load. Browsers often refuse a scheme hop without a user
            gesture, so the button is the path that always works.
            /auth/app/session runs inside the app's webview: it spends the token
            once, sets the normal session cookie there, and lands on the library.
            """
            from urllib.parse import parse_qs
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            token = q.get("token") or ""
            if path == "/auth/app/session":
                try:
                    uid = auth.take_app_handoff(app.conn, token)
                except auth.AuthError:
                    # expired or already spent: the honest next step is the door
                    return self._redirect("/?app=again")
                tok = auth.create_session(app.conn, uid, "app",
                                          ua=self._ua(), ip=self._ip())
                return self._redirect("/", [self._cookie_str(
                    auth.SESSION_COOKIE, tok, max_age=auth.SESSION_TTL)])
            hand = "fireside://auth?token=" + urllib.parse.quote(token)
            page = (
                "<!doctype html><meta charset=utf-8>"
                "<meta name=viewport content='width=device-width,initial-scale=1'>"
                "<title>Back to fireside</title>"
                "<body style=\"margin:0;background:#14110f;color:#ece3d3;"
                "font:16px system-ui,sans-serif;display:flex;min-height:100vh;"
                "align-items:center;justify-content:center;text-align:center\">"
                "<div style='padding:24px;max-width:420px'>"
                "<p>You are signed in.</p>"
                "<p style='color:#b3a794;font-size:14px'>Tap to go back to the app.</p>"
                "<p style='margin-top:22px'><a href=\"" + hand + "\" "
                "style=\"display:inline-block;background:#e3a857;color:#241808;"
                "padding:14px 22px;border-radius:10px;text-decoration:none;"
                "font-weight:600\">Open fireside</a></p></div>"
                "<script>setTimeout(function(){try{location.replace(" +
                json.dumps(hand) + ")}catch(e){}},120)</script>")
            return self._json(200, {"_html": page})

        def _dispatch(self, routes):
            from urllib.parse import urlparse, parse_qs
            u = urlparse(self.path)
            path = u.path.rstrip("/") or "/"
            parts = path.strip("/").split("/")
            # before the /auth/<provider>/<action> branch: "app" is not a provider
            if self.command == "GET" and path in ("/auth/app/session",
                                                  "/auth/app/done"):
                return self._app_auth_route(path, u)
            if (self.command == "GET" and len(parts) == 3 and parts[0] == "auth"):
                return self._auth_route(parts, u)
            if path == "/auth/logout" and self.command == "POST":
                auth.delete_session(app.conn, self._token())
                return self._redirect("/", [self._cookie_str(
                    auth.SESSION_COOKIE, "", max_age=0)])
            if path in ("/auth/local/register", "/auth/local/login") \
                    and self.command == "POST":
                # Unauthenticated and writes a row, so it is the first door a
                # script would knock on. Keyed by address, not by the username
                # it claims, so changing the name buys nothing.
                if not app.door_limiter.allow("auth:" + (self._ip() or "?")):
                    return self._json(429, {"error": "too many attempts"})
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
                except ValueError:
                    return self.send_error(400)
                try:
                    if path.endswith("register"):
                        uid = auth.local_register(app.conn, body.get("username", ""),
                                                  body.get("password", ""),
                                                  body.get("email", ""),
                                                  require_email=app.hosted)
                    else:
                        uid = auth.local_login(app.conn, body.get("username", ""),
                                               body.get("password", ""))
                except auth.AuthError as e:
                    return self._json(401, {"error": str(e)})
                token = auth.create_session(app.conn, uid, "local",
                                            ua=self._ua(), ip=self._ip())
                return self._json(200, {"ok": True, "user": uid,
                                        "admin": auth.is_admin(app.conn, uid)},
                                  cookie=self._cookie_str(
                                      auth.SESSION_COOKIE, token,
                                      max_age=auth.SESSION_TTL))
            # Proving ownership of an account a social login collided with.
            # Both are public by construction: the claim token IS the capability.
            if path == "/auth/claim" and self.command == "GET":
                cq = {k: v[0] for k, v in parse_qs(u.query).items()}
                try:
                    c = auth.take_claim(app.conn, cq.get("token", ""))
                except auth.AuthError as e:
                    return self._json(410, {"error": str(e)})
                m = auth.claim_methods(app.conn, c["user_id"])
                methods = [{"id": pid,
                            "label": app.auth.table.get(pid, {}).get("label", pid),
                            "start": "/auth/%s/start" % pid}
                           for pid in m["providers"]]
                return self._json(200, {
                    "provider": c["provider"], "email": c["email"],
                    "provider_label": (app.auth.table.get(c["provider"], {})
                                       .get("label", c["provider"])),
                    "has_password": m["has_password"],
                    "methods": methods})
            if path == "/auth/claim/confirm" and self.command == "POST":
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
                except ValueError:
                    return self.send_error(400)
                try:
                    uid = auth.complete_claim(app.conn, body.get("claim", ""),
                                              body.get("password", ""))
                except auth.AuthError as e:
                    return self._json(401, {"error": str(e)})
                token = auth.create_session(app.conn, uid, "link",
                                            ua=self._ua(), ip=self._ip())
                return self._json(200, {"ok": True, "user": uid},
                                  cookie=self._cookie_str(
                                      auth.SESSION_COOKIE, token,
                                      max_age=auth.SESSION_TTL))
            # Forgot password: ask for an address, mail a one-time link. The
            # answer is the same whether or not an account matched - telling the
            # two apart would turn this door into "who has an account here?".
            if path == "/auth/forgot" and self.command == "POST":
                # Sends mail: unmetered, it is a free way to make us a spam relay
                # and to enumerate who has an account here.
                if not app.door_limiter.allow("forgot:" + (self._ip() or "?")):
                    return self._json(429, {"error": "too many attempts"})
                from urllib.parse import quote
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
                except ValueError:
                    return self.send_error(400)
                email = (body.get("email") or "").strip().lower()
                res = auth.request_password_reset(app.conn, email)
                if res:
                    tok, username = res
                    link = self._base_url() + "/?reset=" + quote(tok)
                    mailer.send(
                        email, "Reset your fireside password",
                        "Hi %s,\n\nSomeone asked to reset the password for your "
                        "fireside account. If it was you, open this link within "
                        "the hour to set a new one:\n\n%s\n\nIf it was not you, "
                        "ignore this mail - nothing changes.\n\n- fireside\n"
                        % (username, link))
                return self._json(200, {"ok": True, "mail": mailer.configured()})
            # Consume the link: set a new password and sign in with it. The
            # token IS the capability, so no session is needed to reach this.
            if path == "/auth/reset" and self.command == "POST":
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
                except ValueError:
                    return self.send_error(400)
                try:
                    uid = auth.reset_password(app.conn, body.get("token", ""),
                                              body.get("password", ""))
                except auth.AuthError as e:
                    return self._json(401, {"error": str(e)})
                token = auth.create_session(app.conn, uid, "reset",
                                            ua=self._ua(), ip=self._ip())
                return self._json(200, {"ok": True, "user": uid},
                                  cookie=self._cookie_str(
                                      auth.SESSION_COOKIE, token,
                                      max_age=auth.SESSION_TTL))
            # Stripe's server-to-server webhook. Public by necessity: Stripe
            # holds no session, so the signature is the authentication. It is
            # read raw here, before any JSON parse, because the signature is
            # computed over the exact bytes. No session, no auth gate.
            if path.startswith("/webhook/") and self.command == "POST":
                n = int(self.headers.get("Content-Length") or 0)
                payload = self.rfile.read(n) if n else b""
                sig = self.headers.get("Stripe-Signature", "")
                pid = parts[1] if len(parts) > 1 else "stripe"
                status, body = app.payment_webhook(payload, sig, pid)
                return self._json(status, body)
            name = routes.get(path)
            if name is None:
                return self.send_error(404)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            body = {}
            if self.command == "POST":
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    try:
                        body = json.loads(self.rfile.read(n).decode("utf-8"))
                    except ValueError:
                        return self.send_error(400)
            # The session lookup sits outside the per-handler try on purpose: it
            # runs before routing, so it has no handler to hand an error to. But
            # an unguarded raise here closes the connection with no status and no
            # traceback, which reads to a client as "logged out" and to an
            # operator as nothing at all. A lookup that fails is a server fault,
            # so say 503 and log it.
            try:
                q["_user"] = auth.session_user(app.conn, self._token()) or ""
            except Exception as e:
                import traceback
                traceback.print_exc()
                return self._json(503, {"error": "session lookup failed: %s: %s"
                                                 % (type(e).__name__, e)})
            q["_token"] = self._token()
            # three things a handler needs that only the request knows: where
            # this box is reachable (for a return URL we control), who is
            # calling (for the unauthenticated doors), and the caller's
            # idempotency key (so a retried purchase is the same purchase).
            q["_base"] = self._base_url()
            q["_ip"] = self._ip()
            q["_idem"] = (self.headers.get("Idempotency-Key") or "").strip()[:191]
            # language: ?lang= then the fs_lang cookie then Accept-Language
            q["_lang"] = i18n.negotiate(q.get("lang"), self._cookie(i18n.COOKIE),
                                        self.headers.get("Accept-Language"))
            if app.require_auth and path not in PUBLIC_ROUTES and not q["_user"]:
                return self._json(401, {"error": "auth required"})
            try:
                status, payload = getattr(app, name)(q, body)
            except Exception as e:
                return self._json(500, {"error": "%s: %s" % (type(e).__name__, e)})
            if isinstance(payload, dict) and "_html" in payload:
                page = payload["_html"]
                if payload.get("_locale"):
                    page = i18n.inject(page, code=q.get("_lang"))
                raw = page.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            elif isinstance(payload, dict) and "_raw" in payload:
                raw = payload["_raw"]
                self.send_response(status)
                self.send_header("Content-Type", payload.get("_mime", "application/octet-stream"))
                self.send_header("Content-Length", str(len(raw)))
                if payload.get("_cache"):
                    self.send_header("Cache-Control", payload["_cache"])
                self.send_header("X-Checksum", payload.get("checksum", ""))
                self.end_headers()
                self.wfile.write(raw)
            else:
                self._json(status, payload)

        def _json(self, status, payload, cookie=None):
            b = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            self._dispatch(GET_ROUTES)

        def do_POST(self):
            self._dispatch(POST_ROUTES)

        def log_message(self, *args):
            pass

    return Handler


def hosted_from(flag=False):
    """Is this deployment hosted?

    A container says so in its environment, not in its command line, so the
    flag has to be readable both ways. This matters more than it looks: the
    only thing `hosted` turns on today is requiring an email at sign-up, and a
    hosted box that quietly answers False re-opens the hole where a social
    login mints a second account because the first one had no address.
    """
    return bool(flag) or _truthy(os.environ.get("FIRESIDE_HOSTED"))


def serve(app=None, port=8788, host="127.0.0.1"):
    payments.load()      # register the payment provider named by FIRESIDE_PAYMENT_PROVIDER
    app = app or App()
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    return app, httpd


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8788)
    ap.add_argument("--host", default="127.0.0.1",
                    help="127.0.0.1 for this machine; 0.0.0.0 to share on the LAN")
    ap.add_argument("--plugins", default=None)
    ap.add_argument("--storage", default=None,
                    help="storage backend id (default $FIRESIDE_STORAGE, else local)")
    ap.add_argument("--data", default=None,
                    help="data dir for the DB and blobs (default: $FIRESIDE_DATA)")
    ap.add_argument("--hosted", action="store_true")
    ap.add_argument("--require-auth", action="store_true",
                    help="reject requests with no session (default: on)")
    ap.add_argument("--open", action="store_true",
                    help="serve without requiring a session (development only)")
    args = ap.parse_args()
    if args.data:                      # one knob sets DB + blob root alike
        os.environ["FIRESIDE_DATA"] = args.data
    # A session is required unless the operator explicitly opens the server
    # (--open / FIRESIDE_OPEN=1). --require-auth / --hosted still force it on.
    hosted = hosted_from(args.hosted)
    require = args.require_auth or hosted or (not args.open)
    if _truthy(os.environ.get("FIRESIDE_REQUIRE_AUTH")):
        require = True
    if _truthy(os.environ.get("FIRESIDE_OPEN")):
        require = False
    app, httpd = serve(App(plugins_root=args.plugins, storage_id=args.storage,
                           hosted=hosted, require_auth=require),
                       args.port, args.host)
    print("fireside v2 host on http://%s:%d" % (args.host, httpd.server_address[1]))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


