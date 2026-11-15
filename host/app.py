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
import sys
import ipaddress
import json
import os
import re
import socket
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from host.host import Host
import inspect

from host import accounting, allowance, audit, auth, billing, bookmeta, connections, contracts, credentials, db, disambig, draft as drafts, erase, example, grants, i18n, jobs, keepwarm, library, mailer, netguard, notices, paymentapply, payments, playback, plans, pron, quota, ratelimit, reading, renderjob_runner, settings, stats, tags as tagmod
from host.egress import fetch as _fetch

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI_DIR = os.path.join(BASE_DIR, "ui")
ASSETS_DIR = os.path.join(BASE_DIR, "assets")
HOST_VERSION = "2.0"

# The phrase that proves an account with no password really means to delete
# itself. The page's placeholder is translated, so the typed phrase is too: the
# check is on the user's intent, not on English spelling.
DELETE_PHRASES = {"delete my account", "eliminar mi cuenta"}


def _swallowed(where, exc):
    """Say on stderr that a best-effort step failed, and carry on.

    These steps (a cover, a notice, an idempotency row) must never fail the
    request they ride on, but a bare `pass` also hides a real bug behind the
    same silence as "nothing to do".
    """
    print("fireside: %s failed: %s: %s" % (where, type(exc).__name__,
                                           str(exc)[:200]), file=sys.stderr)


def _detail(e, fallback):
    """What the reader is told when ours broke.

    A raw exception was reaching the screen: "OperationalError: SAVEPOINT addbook
    does not exist" arrived as user-facing copy, class name and SQL included. It
    tells a reader nothing they can act on and tells anyone beside them plenty.
    A deliberate refusal in this codebase is a ValueError with a written sentence
    (see host/auth.py), so that passes; anything else is logged and replaced by a
    sentence that says what to do next.
    """
    try:
        print("fireside error: %s: %s" % (type(e).__name__, str(e)[:400]),
              file=sys.stderr)
    except Exception:                                  # noqa: BLE001
        pass
    if isinstance(e, ValueError) and not isinstance(e, (AttributeError, KeyError)):
        text = str(e).strip()
        if text and len(text) <= 200 and "\n" not in text:
            return text
    return fallback


def _byte_range(header, size):
    """The one slice a Range request asks for, or None to send the whole file.

    A browser decides whether it can seek a video by whether the server answers
    a Range with 206: Chrome plays a small clip without it but loops it badly,
    and Safari will not start one at all. Only a single range is honoured. A
    multi-range request answered with one slice would be a lie about the body,
    so it falls back to the whole file instead.
    """
    if not header or size <= 0:
        return None
    spec = header.strip()
    if not spec.lower().startswith("bytes="):
        return None
    spec = spec[6:].strip()
    if "," in spec:
        return None
    first, _, last = spec.partition("-")
    try:
        if not first:                        # bytes=-500: the tail
            n = int(last)
            if n <= 0:
                return None
            return (max(0, size - n), size - 1)
        start = int(first)
        end = int(last) if last else size - 1
    except ValueError:
        return None
    if start < 0 or start >= size or end < start:
        return None
    return (start, min(end, size - 1))


def _norm_confirm(value):
    """Lowercase, trim, and fold accents so 'Eliminar mi Cuenta' counts."""
    text = (value or "").strip().lower()
    folded = unicodedata.normalize("NFKD", text)
    return "".join(c for c in folded if not unicodedata.combining(c))


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


def _ledger_row(r):
    """One ledger row as the wallet screen reads it.

    A plan allowance is stored as a `topup` because it moves the balance the
    same way money does, so kind alone made an included grant and a card
    payment the same row. The id already says which is which (allowance:<u>:<p>,
    welcome:<u>), so the wallet gets a kind it can style without parsing prose.
    Chris, 2026-11-10, on his own wallet: the list had "no visual distinction"
    and one row read "platform allowance for period ending 0".

    The stale note is corrected here too: rows written before the period end
    was formatted still sit in the ledger, and re-reading the ledger must not
    resurrect a number the reader cannot use."""
    rid = str(r.get("id") or "")
    kind = r.get("kind")
    included = False
    if rid.startswith("allowance:"):
        kind, included = "grant", True
    elif rid.startswith("welcome:"):
        kind, included = "welcome", True
    note = r.get("note") or ""
    if included and "period ending 0" in note:
        note = "included with your plan"
    return {"kind": kind, "delta": r.get("delta"),
            "currency": r.get("currency"), "note": note,
            "included": included, "at": r.get("created_at")}


class App:
    def __init__(self, plugins_root=None, db_path=None, storage_id=None,
                 sale_connection=None, rate_limiter=None, hosted=False,
                 require_auth=False):
        self.host = Host(plugins_root)
        self.db_path = db_path
        self.conn = db.connect(db_path)
        library.backfill_renditions(self.conn)   # a book voiced before renditions still lists its voice
        # A fresh boot proves its own request threads are gone, and nothing
        # about a render running in a job or detached process: those only get
        # swept after a long silence.
        jobs.sweep_stale(self.conn, worker="request")
        jobs.sweep_stale(self.conn, stale_seconds=3600, worker="worker")
        # Backend selection lives on the Host so a render job in its own process
        # picks the same one. An explicit id, else $FIRESIDE_STORAGE, else local.
        self.storage_id = self.host.storage_backend(storage_id)
        # Operator settings saved in the DB win over the environment, and this
        # has to happen before the payment provider is registered (serve() does
        # that) or a key saved on the admin screen would be read as absent until
        # the next deploy. See host/settings.py.
        settings.apply(self.conn)
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
        # A render lives in a thread here, so a replica that sleeps loses it.
        # Hold the replica awake for as long as there is work to do.
        if self.hosted:
            keepwarm.start()

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
        except Exception as e:
            # A cache is never allowed to fail the request it is caching.
            _swallowed("idempotency cache", e)

    def _claim_payment(self, event_id, kind, uid, reference, chars=0, detail=""):
        return paymentapply.claim_payment(self.conn, event_id, kind, uid,
                                          reference, chars, detail)

    def _credit_once(self, event_id, kind, uid, reference, chars, detail, credit):
        return paymentapply.credit_once(self.conn, event_id, kind, uid, reference,
                                        chars, detail, credit)

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
        # The durable render switch is configured, not proven: true says this box
        # was told to start renders in a job, not that the start succeeded.
        h["render_job"] = bool((os.environ.get("FIRESIDE_RENDER_JOB") or "").strip())
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
        # The disambiguator declares its server the same way a voice provider
        # does, and the host renders it the same way. No plugin markup.
        out["disambig"] = [{"id": pid, "label": p.label,
                            "settings": self._fields(p.obj)}
                           for pid, p in self.host.disambig.items()]
        # Whether a box is set, which is what decides if the import screen even
        # offers the checkbox. A checkbox that promises nothing is worse than no
        # checkbox, so the page asks instead of assuming.
        out["model_pass"] = bool(self.host.disambig) and disambig.configured()
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

    # ---- disambiguation: ask before audio, never after ---------------------

    def _disambig(self, body, q, uid):
        """Resolve which disambiguator and which credential this request runs on.

        A connection named `disambig:<id>` is kept apart from the voice servers
        because they are usually different machines: the llama box that reads
        and the Kokoro box that speaks. Returns (plugin, cred) or (None, error).
        """
        pid = body.get("provider") or q.get("provider") or ""
        cid = body.get("connection_id") or q.get("connection_id") or ""
        cred = body.get("cred") or {}
        if cid:
            c = connections.get(self.conn, cid)
            if not c or not connections.may_use(
                    c, uid, auth.is_admin(self.conn, uid)):
                return None, (403, {"error": "no such connection"})
            resolved = connections.resolve(self.conn, uid, cid)
            if resolved is None:
                return None, (403, {"error": "connection is switched off"})
            pid, cred = c["provider"].split(":", 1)[-1], resolved
        if not pid:
            pid = next(iter(self.host.disambig), "")
        p = self.host.disambig.get(pid)
        if p is None:
            return None, (400, {"error": "unknown disambiguator"})
        return (p.obj, cred), None

    def disambig_models(self, q, body):
        """What the configured server can run, with prices where we have them.
        An empty list is 'unknown', never 'free'."""
        uid = self._uid(q, body)
        got, err = self._disambig(body, q, uid)
        if err:
            return err
        obj, cred = got
        try:
            models = obj.list_models(cred)
        except Exception:
            models = []
        return 200, {"models": models or []}

    def disambig_quote(self, q, body):
        """Price one pass for this text on this model. The plugin is the
        authority; the host only carries the number."""
        uid = self._uid(q, body)
        got, err = self._disambig(body, q, uid)
        if err:
            return err
        obj, cred = got
        ctx = {"text": body.get("text") or "", "model": body.get("model") or ""}
        qt = obj.quote(ctx)
        out = {"chars_est": qt.chars_est, "price": qt.price,
               "currency": qt.currency, "eligible": qt.eligible,
               "authoritative": qt.authoritative,
               "refusal_reason": qt.refusal_reason}
        return 200, {"quote": out}

    def disambig_scan(self, q, body):
        """The pass itself: text in, the questions that must be answered before
        any audio exists, out. A server that is down is an error, not an empty
        list, because an empty list silently voices the wrong reading."""
        uid = self._uid(q, body)
        got, err = self._disambig(body, q, uid)
        if err:
            return err
        obj, cred = got
        try:
            questions = obj.scan(body.get("text") or body.get("sentences") or [],
                                 body.get("model") or "", cred)
        except Exception as e:
            return 502, {"error": "scan failed",
                        "detail": _detail(e, "the reader could not read that text")}
        return 200, {"questions": questions or []}

    # ---- the model pass: suggestions, never settings ----------------------
    def _disambig_plugin(self):
        """The disambiguator this host actually loaded. Discovery happens once,
        at startup; asking the default plugin directory again here could load a
        second copy that the host never checked the contract against."""
        for p in self.host.disambig.values():
            if getattr(p.obj, "decide", None):
                return p.obj
        return None

    def _own_book(self, uid, book_id):
        """This account owns this book, or None. Every model-pass route goes
        through here: a book id is not a permission."""
        bk = library.get_book(self.conn, book_id) or {}
        if not bk or bk.get("user_id") != uid:
            return None
        return bk

    def _empty_model_state(self):
        return {"book": "", "available": disambig.configured(), "optin": False,
                "status": "", "words": 0, "rules": [], "note": "",
                "updated_at": "", "stale": None}

    def _model_pass_on(self, book_id, user):
        """Allow the pass on one book, and start it now."""
        disambig.set_optin(self.conn, book_id, True)
        return self._start_model_pass(book_id, user)

    def _start_model_pass(self, book_id, user):
        """Run the pass on this book in its own thread, off the request.

        The row, not a job, is the status: the pass belongs to the book and the
        page already polls the book. A second click while one is running is
        ignored rather than paid for twice.
        """
        if not disambig.configured():
            return None
        st = disambig.state(self.conn, book_id) or {}
        if st.get("status") == "running" and not _stale_claim(st.get("updated_at")):
            return None
        disambig._save(self.conn, user, book_id, "running", st.get("rules") or [],
                       st.get("note") or "")

        p = self._disambig_plugin()

        def _work():
            c = db.connect(self.db_path)
            try:
                disambig.scan(c, self.storage, user, book_id,
                              run=getattr(p, "decide", None))
            except Exception as e:
                try:
                    disambig._save(c, user, book_id, "failed", [],
                                   "the pass could not finish: " + str(e)[:200])
                except Exception:
                    pass
            finally:
                c.close()

        threading.Thread(target=_work, daemon=True).start()
        return disambig.state(self.conn, book_id)

    def book_model_get(self, q, body):
        """What the model pass did on this book, and what has gone stale since.

        `stale` is the question Chris asked for (11-11): the rules a chapter was
        voiced with are stamped on the rendition, so the host can say which
        chapters predate the reader's latest word changes without re-reading
        anything by hand.
        """
        uid = self._uid(q, body)
        if not uid:
            # A page with no session yet asks this on load. An empty state is the
            # honest answer; a 401 here would read as an error in the book.
            return 200, self._empty_model_state()
        bid = q.get("book") or ""
        # A route can name a book that is not here (a dead link, a notice whose
        # book was deleted). That is "nothing to say about it", not a refusal:
        # a 403 on a page load is an error nobody can act on.
        if not library.get_book(self.conn, bid):
            out = self._empty_model_state()
            out["book"] = bid
            return 200, out
        if not self._own_book(uid, bid):
            return 403, {"error": "no such book"}
        st = disambig.state(self.conn, bid) or {}
        out = {"book": bid, "available": disambig.configured(),
               "optin": disambig.opted_in(self.conn, bid),
               "status": st.get("status") or "", "words": st.get("words") or 0,
               "rules": st.get("rules") or [], "note": st.get("note") or "",
               "updated_at": st.get("updated_at") or ""}
        try:
            im = pron.impact(self.conn, self.storage, uid, bid,
                             split_for=self._speech_split)
            out["stale"] = {"chapters": im.get("chapters_affected") or 0,
                            "est_seconds": im.get("est_seconds") or 0,
                            "voice": ((im.get("books") or [{}])[0].get("renditions")
                                      or [{}])[0].get("voice_label", "") if im.get("books") else ""}
        except Exception:
            # Not knowing is not "nothing changed". The page says so in words.
            out["stale"] = None
        return 200, out

    def book_model_set(self, q, body):
        """scan | apply | discard | off. Each one is the reader's own hand."""
        uid = self._uid(q, body)
        if not uid:
            return 401, {"error": "auth required"}
        bid = body.get("book") or ""
        if not self._own_book(uid, bid):
            return 403, {"error": "no such book"}
        action = (body.get("action") or "scan").strip().lower()
        if action == "scan":
            if not disambig.configured():
                return 400, {"error": "no model box is set up"}
            disambig.set_optin(self.conn, bid, True)
            st = self._start_model_pass(bid, uid)
            return 200, {"book": bid, "status": (st or {}).get("status", "running"),
                         "optin": True}
        if action == "apply":
            st, err = disambig.apply(self.conn, uid, bid)
            if err:
                return 400, {"error": err}
            return 200, {"book": bid, "status": (st or {}).get("status", ""),
                         "words": (st or {}).get("words", 0), "optin": True}
        if action == "discard":
            st = disambig.discard(self.conn, uid, bid)
            return 200, {"book": bid, "status": (st or {}).get("status", ""),
                         "optin": disambig.opted_in(self.conn, bid)}
        if action == "off":
            disambig.set_optin(self.conn, bid, False)
            return 200, {"book": bid, "optin": False,
                         "status": (disambig.state(self.conn, bid) or {}).get("status", "")}
        return 400, {"error": "unknown action"}

    def ui(self, q, body):
        """Serve the browser client. The host owns its own UI; plugins ship none.

        /pricing is this same page, opened on the price list: one design and
        one checkout, instead of a second static page that drifted from the
        app it was selling.
        """
        with open(os.path.join(UI_DIR, "index.html"), "r", encoding="utf-8") as f:
            page = (f.read().replace("__HOST_VERSION__", HOST_VERSION)
                    .replace("__COMPANY__", os.environ.get("FIRESIDE_COMPANY", "Fireside"))
                    .replace("__CONTACT__", os.environ.get(
                        "FIRESIDE_CONTACT_EMAIL", "support@readfireside.com")))
        # The door's fire is licensed media that may not be in this build yet,
        # so the render says once whether it exists. The alternative is a
        # request on every door load that ends in 404 on a stock checkout.
        hearth = ""
        poster = ""
        if os.path.exists(os.path.join(ASSETS_DIR, "hearth.mp4")):
            hearth = "1"
            if os.path.exists(os.path.join(ASSETS_DIR, "hearth-poster.jpg")):
                poster = ' poster="/hearth-poster.jpg"'
        page = page.replace("__HEARTH_POSTER__", poster).replace("__HEARTH__", hearth)
        # Analytics is the operator's, not the product's: a self-hosted copy
        # ships no tag because there is no id in its environment. The page
        # refuses anything that is not a GA4 measurement id, so a typo is an
        # absent tag rather than a broken one.
        page = page.replace("__GA_ID__", (os.environ.get("FIRESIDE_GA_ID") or "").strip()[:32])
        # localisable: the handler picks the language and splices the catalog in,
        # because only the request knows the cookie and Accept-Language.
        # The theme rides the same render: applied after paint it is a flash of
        # the wrong colours, which is exactly what someone who needs the
        # contrast cannot use.
        return 200, {"_html": page, "_locale": True,
                     "_theme": connections.get_theme(self.conn, (q or {}).get("_user") or "")}

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
                                                      "support@readfireside.com")))
        # Sole operator details, kept in env so a legal page names a real
        # address without hardcoding one in the source. An unset address is
        # said plainly rather than left as a placeholder, and an unset EU
        # representative drops the sentence instead of naming nobody.
        contact = os.environ.get("FIRESIDE_CONTACT_EMAIL", "support@readfireside.com")
        address = (os.environ.get("FIRESIDE_ADDRESS") or "").strip()
        page = page.replace("__ADDRESS__",
                            address or ("available on request from " + contact))
        rep = (os.environ.get("FIRESIDE_EU_REP") or "").strip()
        if rep:
            page = page.replace("__EU_REP__", rep)
        else:
            page = page.replace(
                " Where the law requires us to name a\n  representative in the EU or the UK, it is: __EU_REP__.",
                "")
        return 200, {"_html": page}

    def pricing(self, q, body):
        """The public price list, served as the app opening on its own price view.

        A payment processor asks for pricing that is visible before an account
        exists, so the page cannot sit behind the sign-in screen the way /plans
        used to: a stranger gets the shell with only the price list in it.
        """
        return self.ui(q, body)

    def checkout(self, q, body):
        """The checkout page, live when a provider is.

        A payment processor's reviewer needs to see this page, and so does a
        buyer who was sent here: the plan, the real price, and one button. The
        page never collects a card number; the button opens the provider's own
        form with the price id the server hands it.

        The prices are rendered from the same catalog the app reads, not from
        numbers typed into the page. The static version still quoted the
        launch ladder ($3/$8/$20 a month) after the ladder changed, which is
        the kind of drift a reviewer reads as a bait and switch.
        """
        with open(os.path.join(UI_DIR, "checkout.html"), "r", encoding="utf-8") as f:
            page = f.read()
        page = (page
                .replace("__COMPANY__", os.environ.get("FIRESIDE_COMPANY", "Fireside"))
                .replace("__CONTACT__", os.environ.get("FIRESIDE_CONTACT_EMAIL",
                                                      "support@readfireside.com")))
        pp = payments.get()
        conf = {}
        fn = getattr(pp, "public_config", None)
        if callable(fn):
            try:
                conf = fn() or {}
            except Exception:
                conf = {}
        cat = plans.catalog(pp)
        tiers = {}
        for t in (cat or {}).get("tiers", []):
            if t.get("id") == quota.FREE_PLAN:
                continue
            tiers[t["id"]] = {"label": t.get("label") or t["id"],
                              "month_cents": t.get("list_amount_cents"),
                              "year_cents": t.get("list_annual_cents"),
                              "included_usd": t.get("included_usd"),
                              "included_bytes": t.get("included_bytes")}
        data = {"configured": bool(conf.get("ok")),
                "provider": conf.get("provider") or "",
                "client_token": conf.get("client_token") or "",
                "env": conf.get("env") or "",
                "prices": conf.get("prices") or {},
                "tiers": tiers}
        blob = json.dumps(data, separators=(",", ":")).replace("<", "\\u003c")
        return 200, {"_html": page.replace("__CHECKOUT_JSON__", blob)}

    def privacy(self, q, body):
        """The privacy policy. Public, static, and store-listing mineable."""
        return self._legal("privacy.html")

    def about(self, q, body):
        """What this is, who runs it, and what it costs. Public: a store listing
        and a stranger deciding whether to sign up both need it without an
        account, which is the same reason the price list is public."""
        return self._legal("about.html")

    def kokoro(self, q, body):
        """How to run your own voice server, and connect it here.

        Public, because the person deciding whether fireside is worth paying
        for is the person who needs to know they can run it for nothing. The
        commands are the upstream projects' own; the page says so."""
        return self._legal("kokoro.html")

    def contact(self, q, body):
        """The one address, on a page rather than only at the bottom of a
        policy. Public because the people who need it most are the ones who
        cannot get in."""
        return self._legal("contact.html")

    def terms(self, q, body):
        """The terms of service. Public, static, and store-listing mineable."""
        return self._legal("terms.html")

    def refunds(self, q, body):
        """The refund policy on its own URL.

        Payment processors ask for a refund policy URL at onboarding and a
        reviewer should not have to find the paragraph inside a wall of terms.
        The policy is the same text the terms carry; this page is the front
        door to it."""
        return self._legal("refunds.html")

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

    def _media(self, name, mime):
        """An optional file: absent is an ordinary state, not a 500."""
        try:
            with open(os.path.join(ASSETS_DIR, name), "rb") as f:
                data = f.read()
        except FileNotFoundError:
            return 404, {"error": "no %s in this build" % name}
        return 200, {"_raw": data, "_mime": mime, "_cache": "public, max-age=86400"}

    def loader(self, q, body):
        """The boot mark: a line-art fire, glasses and book that draw in.

        Served as a file rather than inlined, because its own stylesheet uses
        bare class names and would land in the page's global CSS if it were
        pasted in. In an <img> the document is isolated and its
        prefers-reduced-motion query still applies."""
        return self._media("loader.svg", "image/svg+xml")

    def hearth(self, q, body):
        """The fireplace loop behind the door's welcome pane.

        Licensed media the owner drops in, so "not there yet" is a normal
        state: 404, and the door keeps its own glow rather than showing a
        broken player. Swapping the licensed cut in is replacing this one file.
        """
        return self._media("hearth.mp4", "video/mp4")

    def hearth_poster(self, q, body):
        return self._media("hearth-poster.jpg", "image/jpeg")

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
            return e.status, self._refusal(e, user, plugin)
        except Exception as e:
            failed = self._fetch_error(e)
            if failed is None:
                raise
            return failed
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
            return e.status, self._refusal(e, user, plugin)
        except Exception as e:
            failed = self._fetch_error(e)
            if failed is None:
                raise
            return failed
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
        # A file's own subject list becomes opf tags on the shelf, so the
        # import is the one place a book's metadata arrives already filterable.
        subjects = (getattr(book, "meta", None) or {}).get("subjects") or []
        if existing:
            swapped = library.replace_book(self.conn, self.storage, existing, book)
            if subjects:
                tagmod.subjects_into_tags(self.conn, user, existing, subjects)
            self._file_meta(existing, book)
            self._maybe_cover(existing, book)
            if _truthy(body.get("model_pass")):
                self._model_pass_on(existing, user)
            out = {"book_id": existing, "title": getattr(book, "title", ""),
                   "chapters": len(getattr(book, "chapters", [])),
                   "replaced": True}
            out.update(swapped)
            return 200, out
        bid = library.add_book(self.conn, self.storage, user, book,
                               tags_from=subjects)
        self._file_meta(bid, book)
        self._maybe_cover(bid, book)
        # The reader ticked the box on this import, so the pass is allowed on
        # this book and no other. It starts now, in its own thread: a model over a
        # whole book is minutes, and an import is a click.
        if _truthy(body.get("model_pass")):
            self._model_pass_on(bid, user)
        return 200, {"book_id": bid, "title": getattr(book, "title", ""),
                     "chapters": len(getattr(book, "chapters", [])),
                     "model_pass": disambig.opted_in(self.conn, bid)}

    def _refusal(self, e, user, plugin):
        """A plugin's refusal, made useful before it reaches the page.

        A series page is refused because it is several books, and the useful
        answer is the list, not the sentence. Whichever of them this account
        already holds is marked here, from the local shelf, so the picker can
        say so without opening any of them.
        """
        out = e.payload()
        ser = out.get("series")
        if isinstance(ser, dict) and isinstance(ser.get("works"), list):
            for w in ser["works"]:
                if not isinstance(w, dict):
                    continue
                hit = library.find_same(self.conn, user,
                                        {"url": w.get("url")}, plugin.obj)
                if hit:
                    held = library.get_book(self.conn, hit) or {}
                    w["existing"] = {"book_id": hit,
                                     "title": held.get("title") or ""}
        return out

    def import_series(self, q, body):
        """Import the works a person picked out of a series page.

        The list comes from the page, not from the request: this re-reads the
        page through the same reader and imports only URLs that page links to,
        so a picker can never become a way to fetch an arbitrary address. Each
        work is imported by the ordinary import path, one at a time, so every
        rule that applies to a single book (duplicate, quota, replace) applies
        here unchanged; what fails is reported per work instead of failing the
        batch. The books that land are put in one series container, in page
        order, so the shelf reads in the author's order rather than by import
        time.
        """
        user = self._uid(q, body)
        page_url = (body.get("url") or "").strip()
        req = {"kind": "url", "url": page_url}
        plugin = self._pick_reader(req)
        if plugin is None:
            return 400, self._no_reader(req)
        cred = self._source_cred(user, plugin.id, body.get("cred"))
        reader = plugin.obj
        try:
            found = reader.series(req, self._make_fetch(reader, cred), cred)
        except contracts.Refused as e:
            return e.status, self._refusal(e, user, plugin)
        except Exception as e:
            failed = self._fetch_error(e)
            if failed is None:
                raise
            return failed
        if not isinstance(found, dict) or not found.get("works"):
            return 422, {"error": "not a series page",
                         "detail": "that page does not list works of its own"}
        works = found["works"]
        picked = [str(u) for u in (body.get("picks") or [])]
        if not picked:
            return 400, {"error": "nothing picked",
                         "detail": "choose at least one work to pull"}
        by_url = {w["url"]: w for w in works if isinstance(w, dict)}
        # Page order, not request order: the shelf reads in the author's order
        # because this is a series, not a pile of books that arrived together.
        chosen = [w for w in works if isinstance(w, dict) and w["url"] in picked]
        unknown = [u for u in picked if u not in by_url]
        if not chosen:
            return 400, {"error": "nothing picked",
                         "detail": "none of those are on that page",
                         "unknown": unknown}
        done, failed = [], []
        for w in chosen:
            row = {"url": w["url"], "title": w.get("title") or "",
                   "words": w.get("words")}
            # One broken work must not take the batch down with it: a page the
            # site changed, a story that is now a login wall. The rest still land.
            try:
                status, out = self.import_book(
                    q, dict(body, kind="url", url=w["url"]))
            except Exception as e:                    # noqa: BLE001
                row.update({"error": "could not read this work",
                            "detail": _detail(e, "that page would not open")})
                failed.append(row)
                continue
            if status == 200 and out.get("book_id"):
                row.update({"book_id": out["book_id"],
                            "chapters": out.get("chapters"),
                            "replaced": bool(out.get("replaced"))})
                done.append(row)
            elif status == 409 and (out.get("existing") or {}).get("book_id"):
                # already on the shelf: not a failure, and still this series'
                row.update({"book_id": out["existing"]["book_id"],
                           "already": True})
                done.append(row)
            else:
                row.update({"error": out.get("error") or "import failed",
                            "detail": out.get("detail") or ""})
                failed.append(row)
        name = " ".join(str(found.get("title") or "").split())[:120]
        container = None
        if name and done:
            container = self._series_container(user, name,
                                              [r["book_id"] for r in done])
        return 200, {"title": name or "Series", "series": container,
                     "imported": done, "failed": failed,
                     "unknown": unknown}

    def _series_container(self, user, name, book_ids):
        """The series these books belong to, by name: reused if it exists."""
        sid = None
        for s in library.list_series(self.conn, user):
            if (s.get("name") or "").casefold() == name.casefold():
                sid = s["id"]
                break
        if not sid:
            sid = library.create_series(self.conn, user, name)
        if not sid:
            return None
        for bid in book_ids:
            library.set_book_series(self.conn, user, bid, sid)
        return {"id": sid, "name": name, "books": len(book_ids)}

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
                             "detail": self._no_room(user, "the example", incoming)}
            bid = library.add_book(self.conn, self.storage, user, book)
            self._fill_example_details(bid)
            self._maybe_cover(bid, book)
            made = True
        else:
            # A copy added before the bundled record was complete keeps its
            # blanks. Fill those, and only those: a field the reader typed stays
            # their text, and a cover they chose stays their cover.
            if self._fill_example_details(bid):
                self._maybe_cover(bid, example.book())
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

    def _fill_example_details(self, book_id):
        """Write the bundled record onto an example copy, blank fields only.

        Returns True when something was written, so the caller can also put the
        cover on. Never overwrites a field the reader can edit: the example is a
        starting point, not an authority over their shelf."""
        b = library.get_book(self.conn, book_id) or {}
        fill = {k: v for k, v in example.details().items()
                if not (b.get(k) or "").strip()}
        if not fill:
            return False
        library.set_details(self.conn, book_id, **fill)
        return True

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

    def _no_room(self, user_id, what, nbytes):
        """The sentence a refused write gets. On free the honest next step is a
        plan, not a block button, and the screen no longer offers one there."""
        if self._plan(user_id) == quota.FREE_PLAN:
            return ("%s needs about %d bytes and the free plan has no room for "
                    "it. Storage comes with a plan; the wallet pays for "
                    "narration." % (what, nbytes))
        return "%s needs about %d bytes and your plan has no room for it" % (what, nbytes)

    # ---- cover art -----------------------------------------------------------
    # A book's cover is its own image when the reader handed one over (an epub
    # almost always carries it), otherwise it is fetched by ISBN. Both paths are
    # generic: the host has no branch on which reader produced the book.

    COVER_MAX_BYTES = bookmeta.COVER_MAX_BYTES

    def _file_meta(self, book_id, book):
        """Whatever the file itself stated about the book, written down.

        Only fields the file actually carried, and only author and series from
        explicit metadata: dc:creator, calibre:series. A reader's own edit wins,
        so a replacement import never overwrites what someone typed. Absent
        stays absent, which is what makes the scopes honest: a book with no
        author belongs to the unknown bucket rather than to a guess.
        """
        meta = getattr(book, "meta", None) or {}
        if not isinstance(meta, dict):
            return
        row = self.conn.execute("SELECT user_edited FROM books WHERE id=?",
                                (book_id,)).fetchone()
        if row and (row["user_edited"] or 0):
            return
        fields = {}
        for key, col in (("author", "author"), ("series", "series_name"),
                         ("series_index", "series_index"),
                         ("publisher", "publisher"), ("published", "published")):
            v = str(meta.get(key) or "").strip()
            if v:
                fields[col] = v[:255]
        if fields:
            library.set_details(self.conn, book_id, **fields)

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
        except Exception as e:          # a cover is a nicety; never fail an import for it
            _swallowed("cover save", e)

    def _cover_by_isbn(self, isbn):
        """Fetch a cover for an ISBN from a keyless public source, or None."""
        return bookmeta.cover_by_isbn(isbn)

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
                             "detail": self._no_room(owner, "this cover", len(data))}
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
                         "detail": self._no_room(owner, "this cover", len(data))}
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

    BULK_DELETE_MAX = 500

    def delete_books(self, q, body):
        """Remove a picked set from the shelf in one call: the same removal as
        /book/delete, once per book, then one storage recount.

        A book this user does not hold, or one with a conversion still running,
        is left alone and named back in `kept` with its reason - one stuck job
        must not block the other twenty-nine. Nothing partial is hidden: the
        caller gets the count it actually removed."""
        uid = self._uid(q, body)
        ids = []
        for x in (body.get("books") or []):
            x = str(x or "").strip()
            if x and x not in ids:
                ids.append(x)
        if not ids:
            return 400, {"error": "no books named"}
        if len(ids) > self.BULK_DELETE_MAX:
            return 413, {"error": "too many books in one call",
                         "detail": "remove at most %d at a time" % self.BULK_DELETE_MAX}
        removed, kept = [], []
        refs = []
        for book_id in ids:
            b, err = self._owned_book(uid, book_id)
            if err:
                kept.append({"book": book_id, "title": "",
                             "reason": err[1].get("error", "not yours to remove")})
                continue
            if jobs.active_for_book(self.conn, book_id):
                kept.append({"book": book_id, "title": b.get("title", ""),
                             "reason": "a conversion for this book is still running"})
                continue
            refs.extend(library.delete_book_rows(self.conn, book_id))
            playback.discard_book(self.conn, uid, book_id)
            stats.discard_book(self.conn, uid, book_id)
            removed.append({"book": book_id, "title": b.get("title", "")})
        # one sweep for the whole set, after every row is gone: a blob only one
        # of the picked books named is freed here, and a blob one of them still
        # holds is kept. Doing it per book means the same lookups, twice.
        freed = library.gc_blobs(self.conn, self.storage, refs)
        return 200, {"ok": True, "deleted": len(removed), "books": removed,
                     "kept": kept, "freed": freed,
                     "storage": quota.usage(self.conn, self.storage, uid)}

    def _url_allowed(self, url):
        """True only for an http(s) URL whose host resolves to a public address.
        A cover URL is user input, so it must never reach this box's own
        network or a file on it."""
        return netguard.is_public_url(url)

    def _cover_from_url(self, url):
        return bookmeta.cover_from_url(url, allowed=self._url_allowed)

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

    def _get_json(self, url):
        return bookmeta.get_json(url)

    @staticmethod
    def _norm_isbn(isbn):
        return bookmeta.norm_isbn(isbn)

    @staticmethod
    def _valid_isbn(isbn):
        return bookmeta.valid_isbn(isbn)

    def _details_by_isbn(self, isbn):
        """Public metadata for an ISBN from OpenLibrary, or None."""
        return bookmeta.details_by_isbn(isbn, get_json=self._get_json)

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
                except Exception as e:
                    _swallowed("cover save", e)
        return 200, {"ok": True, "book": self._book_payload(book_id)}

    def _book_payload(self, book_id):
        b = library.get_book(self.conn, book_id) or {}
        meta = b.get("meta") or {}
        return {"id": book_id, "title": b.get("title", ""),
                "author": b.get("author", ""), "cover": b.get("cover", ""),
                # where the work came from, so the play page can credit it: the
                # reader named both when it read the book, and this is only a
                # pass-through of that (an upload has neither).
                "url": meta.get("url", "") or "",
                "source": meta.get("source", "") or "",
                "isbn": b.get("isbn", ""), "publisher": b.get("publisher", ""),
                "published": b.get("published", ""),
                "description": b.get("description", ""),
                "user_edited": b.get("user_edited", 0),
                "series": library.series_of(self.conn, book_id),
                "tags": tagmod.for_user(self.conn, b.get("user_id") or "")
                              .get(book_id, [])}

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

    def book_read_set(self, q, body):
        """Mark a book read or unread. Nothing is charged and no text moves: it
        is the reader's own shelf state, which is why it lives on the book the
        reader owns and not on the work."""
        book_id = body.get("book", "")
        b = library.get_book(self.conn, book_id)
        if not b or b.get("user_id") != self._uid(q, body):
            return 404, {"error": "no such book"}
        library.set_read(self.conn, book_id, bool(body.get("read", True)))
        return 200, {"ok": True, "read": library.is_read(self.conn, book_id)}

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

    def tags_get(self, q, body):
        """The shelf's vocabulary: every tag in use and how many books carry it.

        One call, because the filter chips are a list of words and their
        counts; asking per book would make the screen wait on the library.
        """
        user = self._uid(q, None)
        return 200, {"tags": [{"tag": t, "books": n}
                              for t, n in tagmod.counts(self.conn, user).items()]}

    def tags_set(self, q, body):
        """Add or remove one tag on one book.

        `op` is add or remove. Removal of a tag the file itself declared hides
        it rather than deleting it, so the next import cannot put it back.
        """
        user = self._uid(q, body)
        book_id = (body or {}).get("book", "")
        tag = (body or {}).get("tag", "")
        op = (body or {}).get("op", "add")
        if not user or not book_id:
            return 400, {"error": "book required"}
        if op == "remove":
            ok = tagmod.remove(self.conn, user, book_id, tag)
        else:
            got = tagmod.add(self.conn, user, book_id, tag,
                             source=(body or {}).get("source") or "user")
            ok = bool(got)
        return 200, {"ok": ok, "book": self._book_payload(book_id),
                     "tags": tagmod.for_user(self.conn, user).get(book_id, [])}

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
            except Exception as e:      # one broken reader must not hide the rest
                _swallowed("reader %s matches()" % getattr(p, "id", "?"), e)
                continue
        return None

    def _fetch_error(self, e):
        """(status, body) for a link that could not be fetched, else None.

        These are the user's link failing, not the server: a refused address is
        theirs to fix (400) and an unreachable or erroring site is upstream (502).
        Anything else is a bug and stays a 500.
        """
        if isinstance(e, netguard.BlockedAddress):
            return 400, {"error": str(e)}
        if isinstance(e, urllib.error.HTTPError):
            return 502, {"error": "could not fetch that link (HTTP %d)" % e.code}
        if isinstance(e, (OSError, TimeoutError)):
            return 502, {"error": "could not reach that link"}
        if isinstance(e, RuntimeError) and str(e).startswith("HTTP "):
            return 502, {"error": "could not fetch that link (%s)"
                                  % str(e).split(" from ")[0]}
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
        if not (voice or "").strip():
            # An empty voice is not "the default one": every speech provider has
            # its own idea of a default, so a render that arrives without a voice
            # produces audio nobody chose. The dialog can send this while its
            # voice list is still loading, and the result looks like a bug in the
            # book rather than a missing choice, so it is refused here where any
            # client hears it.
            return 400, {"error": "no voice chosen",
                         "detail": "pick a voice first; the list may still be loading"}
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
                         "detail": self._no_room(user, "this render", est_bytes)}
        sale = accounting.sale_connection_matches(self.sale_connection, provider, voice,
                                                  model, cid)
        # What this run actually has to synthesise: a chapter already voiced in
        # this exact voice (and rules) is a cache hit and costs the buyer
        # nothing. The whole-book estimate is the storage question, not the
        # money question, so the two are kept apart.
        rules = pron.for_book(pron.load(self.conn, user), book_id,
                              pron.book_author(self.conn, book_id),
                              pron.book_series(self.conn, book_id))
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
                allowance.welcome(self.conn, user)
                allowance.due(self.conn, user)
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
                             total=len(todo), status="queued",
                             connection_id=cid, speed=speed, only=only)
        if res_id:
            # The row carries what a worker in another process needs to settle
            # this charge: the reservation and the characters it quoted.
            jobs.set_reservation(self.conn, jid, res_id, fresh)
        opts = {"model": model, "speed": speed}

        def _work():
            # A render can run for minutes; it gets its own DB connection so it
            # never blocks the request thread and the client can poll /job.
            c = db.connect(self.db_path)
            try:
                jobs.render_book(c, self.storage, speech, user, book_id, voice,
                                 model, speed, cred=cred, opts=opts, only=only,
                                 job_id=jid, voice_label=voice_label,
                                 plugins_root=self.host.plugins_root)
                if sale and res_id:
                    # Charge only the characters this run actually synthesised:
                    # what is still stale after the run is what was never voiced
                    # (a chapter that was already cached, or one that failed).
                    # Shared with the out-of-process render job, which settles
                    # the same reservation from its own process.
                    accounting.settle_run(c, self.storage, speech, book_id, todo,
                                          rules, voice, model, speed, res_id,
                                          fresh, cred, jid)
                self._notice_run(c, user, book_id, jid)
            finally:
                c.close()

        # Out of process when this box can do it (a Container Apps Job on the
        # hosted deployment, a detached worker anywhere else), because a thread
        # dies with the request process. In process only as the fallback, and
        # then only one of the two runs: start() says whether something took it.
        mode = renderjob_runner.start(jid)
        if mode:
            jobs.set_worker(self.conn, jid, "worker")
        else:
            threading.Thread(target=_work, daemon=True).start()
        return 200, {"job_id": jid, "total": len(todo)}

    def _notice_run(self, c, user, book_id, jid):
        """Tell the bell how a run ended. The job row is the record; this reads
        it back rather than guessing, because render_book reports failure by
        writing a status, not by raising. A cancelled run is the reader's own
        doing and gets no notice."""
        try:
            j = jobs.job(c, jid) or {}
            st = j.get("status")
            if st not in ("done", "error"):
                return
            r = c.execute("SELECT title FROM books WHERE id=?", (book_id,)).fetchone()
            title = ((r["title"] if r else "") or "").strip() or "your book"
            route = "#/play/" + str(book_id)
            if st == "done":
                notices.notify(c, user, "job", "%s is ready to play." % title,
                               route=route, key="run:" + jid)
            else:
                notices.notify(c, user, "job", "Could not convert %s. %s"
                               % (title, (j.get("error") or "").strip()[:240]),
                               route=route, key="run:" + jid)
        except Exception as e:             # a notice never costs a finished run
            _swallowed("run notice", e)

    # ---- notifications: what happened while you were elsewhere ---------------
    def notices_list(self, q, body):
        """This user's notices, newest first. The unread count travels with the
        list so the bell can badge itself without a second request."""
        uid = self._uid(q, body)
        if not uid:
            return 401, {"error": "auth required"}
        try:
            n = max(1, min(50, int(((q or {}).get("n") or ["20"])[0])))
        except (TypeError, ValueError):
            n = 20
        return 200, {"items": notices.list_for(self.conn, uid, n),
                     "unread": notices.unread(self.conn, uid)}

    def notices_read(self, q, body):
        """Mark notices read: the listed ids, or all of them."""
        uid = self._uid(q, body)
        if not uid:
            return 401, {"error": "auth required"}
        body = body or {}
        ids = body.get("ids")
        unread = notices.mark_read(self.conn, uid, ids if isinstance(ids, list) else None)
        return 200, {"ok": True, "unread": unread}

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
        allowance.welcome(self.conn, uid)
        allowance.due(self.conn, uid)
        out = {
            "balance": accounting.balance(self.conn, uid),
            "reserved": accounting.reserved_total(self.conn, uid),
            "available": accounting.available(self.conn, uid),
            # Read the unit, do not assume it: during the switch to dollars an
            # old account still holds characters, and printing "chars" over a
            # dollar balance (or the reverse) is the one lie a wallet cannot
            # afford.
            "unit": accounting.currency(self.conn, uid) or "USD",
            "payments_configured": payments.configured(),
            "sale": None,
        }
        provider, speech, cred = self._sale_connection(uid)
        if speech is not None:
            out["sale"] = _sale_offer(provider, speech, cred)
        return 200, out

    def credits_history(self, q, body):
        """The last rows of this user's own ledger, newest first.

        The wallet is the one place where money changing is visible, so an
        absorbed overage has to be a row a person can read - estimate against
        final - rather than a line in a support log. Read-only."""
        uid = self._uid(q, body)
        if not uid:
            return 401, {"error": "auth required"}
        rows = accounting.ledger_entries(self.conn, uid)[-50:]
        rows.reverse()
        return 200, {
            "unit": accounting.currency(self.conn, uid) or "USD",
            "entries": [_ledger_row(r) for r in rows],
        }

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
        credited = {}

        def credit(entry_id):
            credited["total"] = accounting.topup(
                self.conn, uid, chars, currency="chars", entry_id=entry_id,
                note="purchase %.2f USD -> %d chars (%s)"
                     % (usd, chars, receipt.reference))
        if not self._credit_once(receipt.reference, "characters", uid,
                                 receipt.reference, chars, "direct charge",
                                 credit):
            return 409, {"error": "this payment was already credited",
                         "reference": receipt.reference,
                         "balance": accounting.balance(self.conn, uid)}
        total = credited["total"]
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
        fresh = self._credit_once(
            p.reference, "characters", uid, p.reference, p.chars,
            "checkout confirmed",
            (lambda eid: accounting.topup(
                self.conn, uid, p.chars, currency="chars", entry_id=eid,
                note="purchase %d chars (%s)" % (p.chars, p.reference)))
            if p.chars else None)
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
        sub = billing.get(self.conn, uid) if uid else None
        return 200, {"subscription": sub,
                     "plans": plans_list,
                     "payments_configured": payments.configured(),
                     # Access is a decision, not a status word: it includes the
                     # lapsed-period check. The screen shows "manage billing"
                     # only when there is a provider customer to open.
                     "access": billing.active(self.conn, uid) if uid else False,
                     "billing_account": bool((sub or {}).get("customer"))}

    def plans_get(self, q, body):
        """The offering, for the plan screen: the tiers and the storage add-on,
        with whatever prices a payment provider reports (None until one is set)."""
        return 200, plans.catalog(payments.get())

    def billing_config(self, q, body):
        """The browser's half of checkout: a client token and the price ids.

        Public on purpose and deliberately narrow. A provider whose card form is
        a page overlay (Paddle) needs the browser to know which price to open;
        everything that can move money stays server-side. With no provider
        registered the honest answer is ok=false, and the page keeps its
        "not for sale" state instead of opening an overlay that cannot charge.
        """
        pp = payments.get()
        fn = getattr(pp, "public_config", None)
        data = fn() if callable(fn) else {
            "ok": False, "provider": "", "env": "", "prices": {},
            "currency": "USD", "detail": "no payment provider configured"}
        data["payments_configured"] = payments.configured()
        return 200, data

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
        # Storage is what a plan buys; the wallet does not. A free account may
        # put dollars in the wallet and spend them on narration, but a block of
        # room is not for sale on free, because that room is the reason to move
        # up. Chris, 11-09. The screen hides the button too; this is the gate.
        wanted = [it["plan"] for it in (meta.get("items") or [])] or [plan]
        upgrades = [p for p in wanted if p not in (quota.STORAGE_ADDON, quota.FREE_PLAN)]
        sub = billing.get(self.conn, uid) or {}
        if (quota.STORAGE_ADDON in wanted and not upgrades
                and sub.get("plan") == quota.FREE_PLAN):
            return 402, {"error": "a plan is needed for storage",
                         "detail": "Storage comes with a plan. The wallet pays for "
                                   "narration on free; room on the shelf starts with "
                                   "the Reader plan."}
        co = _with_idem(fn, plan, meta, idem=idem)
        if not getattr(co, "ok", False):
            return 402, {"error": "checkout refused",
                         "detail": getattr(co, "detail", "")}
        payload = {"url": co.url, "reference": co.reference}
        self._idem_put(uid, idem, 200, payload)
        return 200, payload

    def _apply_subscription(self, uid, sub, ref, provider_id):
        return paymentapply.apply_subscription(self.conn, uid, sub, ref, provider_id)

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

    def billing_storage(self, q, body):
        """Add blocks to the storage subscription the account already has.

        Every Paddle checkout opens a new subscription, so the card form is the
        right door only for the FIRST block. A second block through the same
        door used to create a second $2 subscription and then overwrite the
        add-on row with quantity 1 - two charges, one block of room (bought
        twice in sandbox, 2026-11-13). Here the quantity is raised on the one
        subscription instead, and the provider is asked before the row moves.
        """
        uid = self._uid(q, body)
        if not uid:
            return 401, {"error": "auth required"}
        if not self._limit(self.buy_limiter, uid):
            return 429, {"error": "rate limited"}
        pp = payments.get()
        fn = getattr(pp, "add_storage", None) if pp is not None else None
        if not callable(fn):
            return 503, {"error": "payments not configured"}
        try:
            qty = int((body or {}).get("quantity") or 1)
        except (TypeError, ValueError):
            qty = 1
        qty = max(1, min(50, qty))
        sub = billing.get(self.conn, uid) or {}
        if (sub.get("plan") or "") in ("", quota.FREE_PLAN):
            return 402, {"error": "a plan is needed for storage",
                         "detail": "Storage comes with a plan. The wallet pays for "
                                   "narration on free; room on the shelf starts with "
                                   "the Reader plan."}
        a = billing.addons(self.conn, uid).get(billing.STORAGE_ADDON) or {}
        sid = (a.get("subscription_id") or "").strip()
        if not sid:
            # No block subscription yet: the card form has to open one, and that
            # is the browser's job, not this route's. The page uses this answer
            # to know which door to take.
            return 409, {"error": "no storage subscription yet",
                         "detail": "the card form opens the first block",
                         "first_block": True}
        try:
            current = int(a.get("quantity") or 0)
        except (TypeError, ValueError):
            current = 0
        res = fn(sid, current + qty)
        if not res.get("ok"):
            return 402, {"error": "storage update refused",
                         "detail": res.get("detail", "")}
        row = billing.upsert_addon(
            self.conn, uid, billing.STORAGE_ADDON,
            int(res.get("quantity") or (current + qty)),
            a.get("status") or "active", provider="paddle",
            reference=a.get("reference") or sid,
            current_period_end=a.get("current_period_end") or 0,
            customer=a.get("customer") or "", subscription_id=sid,
            cancel_at=a.get("cancel_at") or 0)
        return 200, {"ok": True, "addon": row}

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
            end = res.get("current_period_end") or sub["current_period_end"]
            # Paddle keeps the subscription active and schedules the change, so
            # the row has to carry the date itself or the next load of the
            # account screen would say "renews on ..." to someone who cancelled.
            billing.upsert(self.conn, uid, sub["plan"], sub["status"],
                           provider=sub["provider"], reference=sub["reference"],
                           current_period_end=end,
                           customer=sub.get("customer", ""),
                           subscription_id=sid,
                           cancel_at=end)
            notices.notify(self.conn, uid, "billing",
                           "Renewal is stopping. You keep everything until this period ends.",
                           route="#/settings/billing")
        # Storage is what a plan buys, so a block subscription outliving its
        # plan is a charge for room the account is no longer allowed to buy.
        # Stopping it here keeps the two in step; a refusal is reported and
        # does not undo the plan cancel.
        storage_cancel = {}
        a = billing.addons(self.conn, uid).get(billing.STORAGE_ADDON) or {}
        asid = (a.get("subscription_id") or "").strip()
        if asid:
            r2 = fn(asid)
            storage_cancel = {"ok": bool(r2.get("ok")),
                              "detail": r2.get("detail", "")}
            if r2.get("ok"):
                end2 = r2.get("current_period_end") or a.get("current_period_end") or 0
                billing.upsert_addon(
                    self.conn, uid, billing.STORAGE_ADDON,
                    a.get("quantity") or 0, a.get("status") or "",
                    provider=a.get("provider") or "paddle",
                    reference=a.get("reference") or asid,
                    current_period_end=end2, customer=a.get("customer") or "",
                    subscription_id=asid, cancel_at=end2)
        return 200, {"ok": True, "cancel_at_period_end":
                     bool(res.get("cancel_at_period_end")),
                     "current_period_end": res.get("current_period_end", 0),
                     "storage_cancel": storage_cancel,
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

    def billing_change(self, q, body):
        """Move a paid subscription onto another tier, without a second one.

        Buying a plan through checkout while already subscribed would open a
        second subscription, which is two charges and two cancels to think
        about. The provider changes the existing one, and the webhook it sends
        back is what writes the new tier here.
        """
        uid = self._uid(q, body)
        if not uid:
            return 401, {"error": "auth required"}
        if not self._limit(self.buy_limiter, uid):
            return 429, {"error": "rate limited"}
        pp = payments.get()
        fn = getattr(pp, "change_plan", None)
        if not callable(fn):
            return 503, {"error": "payments not configured"}
        sub = billing.get(self.conn, uid)
        sid = (sub or {}).get("subscription_id") or ""
        if not sid:
            return 409, {"error": "no subscription to change",
                         "detail": "pick a plan first; a change moves the one you have"}
        plan = ((body or {}).get("plan") or "").strip()
        if plan not in quota.PAID_TIERS:
            return 400, {"error": "not a paid plan", "detail": plan}
        if plan == sub["plan"]:
            return 409, {"error": "already on that plan"}
        if sub.get("cancel_at"):
            # Changing items on a subscription that is scheduled to end would
            # charge a prorated amount for a period it is not going to have.
            return 409, {"error": "renewal is stopping",
                         "detail": "keep the renewal going first, then change plan"}
        res = fn(sid, plan, (body or {}).get("cycle") or "")
        if not res.get("ok"):
            return 402, {"error": "change refused",
                         "detail": res.get("detail", "")}
        notices.notify(self.conn, uid, "billing",
                       "Your plan is now %s. The difference was prorated today." % plan,
                       route="#/settings/billing")
        return 200, {"ok": True, "plan": plan,
                     "cycle": res.get("cycle", ""),
                     "subscription": billing.get(self.conn, uid)}

    def billing_resume(self, q, body):
        """Put the next renewal back after a scheduled cancel."""
        uid = self._uid(q, body)
        if not uid:
            return 401, {"error": "auth required"}
        if not self._limit(self.buy_limiter, uid):
            return 429, {"error": "rate limited"}
        pp = payments.get()
        fn = getattr(pp, "resume", None)
        if not callable(fn):
            return 503, {"error": "payments not configured"}
        sub = billing.get(self.conn, uid)
        sid = (sub or {}).get("subscription_id") or ""
        if not sid:
            return 409, {"error": "no subscription on file"}
        if not sub.get("cancel_at"):
            return 200, {"ok": True, "detail": "nothing was scheduled to stop"}
        res = fn(sid)
        if not res.get("ok"):
            return 402, {"error": "resume refused",
                         "detail": res.get("detail", "")}
        # The provider said the scheduled change is gone, so record that.
        billing.upsert(self.conn, uid, sub["plan"], sub["status"],
                       provider=sub["provider"], reference=sub["reference"],
                       current_period_end=sub["current_period_end"],
                       customer=sub.get("customer", ""),
                       subscription_id=sid, cancel_at=0)
        notices.notify(self.conn, uid, "billing", "Your plan keeps renewing.",
                       route="#/settings/billing")
        return 200, {"ok": True, "subscription": billing.get(self.conn, uid)}

    def _apply_subscription_state(self, st, provider_id):
        return paymentapply.apply_subscription_state(self.conn, st, provider_id)

    def _credit_wallet_tx(self, pp, tx, cust=None):
        return paymentapply.credit_wallet_tx(self.conn, pp, tx, cust)

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
        norm = getattr(pp, "normalize_event", None)
        if callable(norm):
            # A provider whose events ARE the state (Paddle) hands the core one
            # normalized shape and the core mirrors it. There is no session to
            # re-read and no price is computed here, only what Paddle said.
            # Idempotency is the event id, so a retry or a replay is a no-op.
            out = billing.apply_event(self.conn, norm(event) or {})
            if out.get("kind") == "transaction" and out.get("transaction"):
                out["wallet"] = self._credit_wallet_tx(pp, out["transaction"],
                                                       out.get("customer"))
            return 200, out
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
            fresh = self._credit_once(
                p.reference, "characters", p.user, p.reference, p.chars,
                "webhook",
                (lambda eid: accounting.topup(
                    self.conn, p.user, p.chars, currency="chars", entry_id=eid,
                    note="purchase %d chars (%s)" % (p.chars, p.reference)))
                if p.chars and p.user else None)
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
        try:
            audio = speech.synth(text, voice, {"model": model, "speed": speed}, cred)
        except Exception as e:
            # The caller's own voice server (a wrong key, a dead host) is theirs
            # to read about; it is the sandbox's whole point. Not a server fault.
            return 502, {"error": "the voice server failed", "detail": str(e)[:300]}
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
                           "shown": f.get("shown", True) is not False,
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
        if "theme" in body:
            kw["theme"] = body.get("theme") or ""
        connections.set_prefs(self.conn, uid, **kw)
        return 200, {"ok": True, "theme": connections.get_theme(self.conn, uid)}

    # ---- pronunciation fixes (per user, applied before any engine) ----------

    def pron_get(self, q, body):
        uid = self._uid(q, body)
        if not uid:
            return 200, {"rules": [], "model": []}
        own = pron.mine(self.conn, uid)
        # The editor shows what the reader typed. A model pass writes its guesses
        # at the same scopes, and showing them in this list makes them look like
        # the reader's own words (and a Save would write them into one). They are
        # handed over separately, with the dictionary id so they can be dropped.
        guesses = [{"id": d["id"], "scope": d["scope"], "scope_key": d["scope_key"],
                    "count": len(d["rules"])}
                   for d in pron.dicts(self.conn, uid) if d["name"] == pron.MODEL_DICT_NAME]
        return 200, {"rules": pron.fold(own), "model": guesses}

    def pron_set(self, q, body):
        uid = self._uid(q, body)
        if not uid:
            return 401, {"error": "auth required"}
        pron.save(self.conn, uid, body.get("rules"))
        # same shape as GET: the editor's list is the reader's words only
        return 200, {"ok": True, "rules": pron.fold(pron.mine(self.conn, uid))}

    def pron_dicts(self, q, body):
        """Every dictionary this user has, with its scope and words."""
        uid = self._uid(q, body)
        if not uid:
            return 401, {"error": "auth required"}
        one = q.get("id") or ""
        if one:
            got = pron.export_dict(self.conn, uid, one)
            if not got:
                return 404, {"error": "no such dictionary"}
            return 200, got
        return 200, {"dicts": pron.dicts(self.conn, uid),
                     "scopes": list(pron.SCOPES),
                     "unknown": {"author": pron.UNKNOWN_AUTHOR,
                                 "series": pron.UNKNOWN_SERIES}}

    def pron_dict_save(self, q, body):
        """Create or update one dictionary, or import one from a file."""
        uid = self._uid(q, body)
        if not uid:
            return 401, {"error": "auth required"}
        if body.get("import"):
            got = pron.import_dict(self.conn, uid, body.get("dict") or body.get("rules"),
                                   body.get("scope"), body.get("scope_key"),
                                   body.get("name") or "")
            if not got:
                return 400, {"error": "not a dictionary file"}
            return 200, {"ok": True, "dict": got}
        got = pron.save_dict(self.conn, uid, body.get("name") or "",
                             body.get("scope") or "global",
                             body.get("scope_key") or "",
                             body.get("rules") or [],
                             body.get("id") or "")
        if not got:
            return 400, {"error": "a scope needs a name to attach to"}
        return 200, {"ok": True, "dict": got}

    def pron_dict_delete(self, q, body):
        uid = self._uid(q, body)
        if not uid:
            return 401, {"error": "auth required"}
        did = body.get("id") or q.get("id") or ""
        if not did:
            return 400, {"error": "which dictionary?"}
        pron.delete_dict(self.conn, uid, did)
        return 200, {"ok": True}

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

    # ---- balance adjustments (admin) ----------------------------------------
    # Support and comps: a specific amount of credit on one account, added or
    # taken back, with a name on the row. A plan comp and a limit grant already
    # exist; this is the missing third thing, because neither of those puts a
    # dollar on a balance and the screen had no route that did.

    def admin_credit(self, q, body):
        """Admin: add or remove credit on one account.

        `amount` is signed: +5 adds five dollars, -5 takes five back. Every
        attempt writes one audit line, applied or refused, so a balance that did
        not move still has evidence that someone tried. Both balances come back
        on success, so the caller sees the number it produced.
        """
        uid = self._admin_only(q)
        if not uid:
            return 403, {"error": "admin only"}
        body = body or {}
        target = (body.get("user") or "").strip()
        who = self._account_name(uid)
        ip = (q or {}).get("_ip") or ""

        def refused(code, err, amount=0.0):
            audit.record(self.conn, uid, who, "adjust", target, amount,
                         outcome="refused", reason=err, ip=ip)
            return code, {"error": err}

        if not target:
            return refused(400, "user required")
        row = self.conn.execute("SELECT id FROM users WHERE id=?", (target,)).fetchone()
        if not row:
            return refused(404, "no such user")
        try:
            amount = float(body.get("amount"))
        except (TypeError, ValueError):
            return refused(400, "amount must be a number")
        if amount == 0:
            return refused(400, "amount must not be zero")
        # A fat-fingered 500 instead of 5 is the one mistake this screen invites,
        # so one entry is capped. Two entries are allowed and say so.
        if abs(amount) > 1000:
            return refused(400, "one entry may be at most 1000 "
                                "(do it in two entries if it is real)", amount)
        amount = round(amount, 2)
        reason = re.sub(r"\s+", " ", (body.get("note") or "")).strip()[:200]
        note = "%s %s%s" % (("credit by %s" % who) if amount > 0
                            else ("debit by %s" % who),
                            ("%.2f" % abs(amount)),
                            (": " + reason) if reason else "")
        before = accounting.balance(self.conn, target)
        try:
            after = accounting.adjust(self.conn, target, amount, note=note)
        except accounting.BelowZero as e:
            return refused(400, "would take the balance below zero", amount)
        audit.record(self.conn, uid, who, "adjust", target, amount,
                     before=before, after=after, outcome="applied",
                     note=reason, ip=ip)
        return 200, {"ok": True, "user": target, "amount": amount,
                     "before": round(before, 2), "balance": round(after, 2),
                     "currency": accounting.currency(self.conn, target) or "USD",
                     "note": note}

    def admin_audit(self, q, body):
        """Admin: the staff action log, newest first."""
        if not self._admin_only(q):
            return 403, {"error": "admin only"}
        limit = min(int((q or {}).get("limit") or 20), audit.MAX)
        return 200, {"entries": audit.recent(self.conn, limit)}

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
                     "tiers": list(quota.TIERS)}

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
        tiers = list(quota.TIERS)
        if not target:
            return 400, {"error": "user required"}
        if plan not in tiers:
            return 400, {"error": "plan must be one of %s" % (tiers,)}
        row = billing.upsert(self.conn, target, plan, "active", provider="admin")
        return 200, {"ok": True, "subscription": row}

    # ---- payments setup (admin) ---------------------------------------------
    # Going live with the card provider used to be a paste-the-secrets-into-Azure
    # job, which meant the values were invisible, unvalidated, and could only
    # change with a new image. These three routes make it a screen: read what is
    # set (never the secrets themselves), save a batch, and ask the provider
    # whether the saved values are actually real.

    def _admin_only(self, q):
        uid = (q or {}).get("_user") or ""
        return uid if auth.is_admin(self.conn, uid) else ""

    def admin_billing(self, q, body):
        """Admin: the provider configuration, masked, and what still blocks a
        sale. A secret comes back as set:true plus its last four characters."""
        if not self._admin_only(q):
            return 403, {"error": "admin only"}
        out = settings.view(self.conn)
        out["provider"] = getattr(payments.get(), "id", "")
        out["payments_configured"] = payments.configured()
        return 200, out

    def admin_billing_save(self, q, body):
        """Admin: save a batch of settings and put them to work immediately.

        Validated as a batch before anything is written, so a refused save (a
        live key against the sandbox environment, two tiers sharing one price)
        leaves the working configuration alone. Applied and re-registered in
        the same breath: a key that is saved but not loaded is the shape of bug
        that has cost this project a live render more than once.
        """
        if not self._admin_only(q):
            return 403, {"error": "admin only"}
        body = body or {}
        values = body.get("values")
        if values is None:
            values = {k: v for k, v in body.items() if k in settings.BY_KEY}
        if not isinstance(values, dict):
            return 400, {"error": "values must be an object"}
        clear = [k for k in (body.get("clear") or []) if isinstance(k, str)]
        saved, errors = settings.save(self.conn, values, clear=clear)
        if errors:
            return 400, {"error": "not saved", "fields": errors,
                         "settings": settings.view(self.conn)}
        settings.apply(self.conn)
        payments.reload()
        out = settings.view(self.conn)
        out.update({"ok": True, "saved": saved,
                    "payments_configured": payments.configured(),
                    "provider": getattr(payments.get(), "id", "")})
        return 200, out

    def admin_billing_check(self, q, body):
        """Admin: ask Paddle whether the saved key and price ids exist.

        A saved value is a claim. This is the receipt: the key authenticates,
        the catalog holds every id we sell, and the webhook secret is stated as
        unproven, because only a real delivery can prove it.
        """
        if not self._admin_only(q):
            return 403, {"error": "admin only"}
        return 200, settings.check(self.conn)

    def admin_accounts(self, q, body):
        """Admin: one page of accounts as rows, with the numbers that decide what
        to do about each one.

        The admin screen used to stack three separate lists (grants, plans,
        sessions) that each repeated every account, then it became one long page
        of accordions. Both showed everything at once, so a server with fifty
        accounts was unreadable and every row paid for storage and stats whether
        or not anyone looked at it. This is a control: `offset`/`limit` pick the
        page, `filter` narrows the roster, and the expensive numbers are only
        measured for the rows actually returned. `total` is what the pager needs
        and is computed before any of that.

        Everything here is measured, per account: what the shelf holds, what has
        been voiced, how long it has been listened to, when it was last seen.
        A missing row is zero, never a guess.
        """
        uid = (q or {}).get("_user") or ""
        if not auth.is_admin(self.conn, uid):
            return 403, {"error": "admin only"}
        q = q or {}
        try:
            limit = int(q.get("limit") or 10)
        except (TypeError, ValueError):
            limit = 10
        try:
            offset = int(q.get("offset") or 0)
        except (TypeError, ValueError):
            offset = 0
        limit = max(1, min(limit, 100))
        offset = max(0, offset)
        want = (q.get("filter") or "").strip().lower()

        admins = {r["user_id"] for r in
                  self.conn.execute("SELECT user_id FROM admins")}
        named = {}
        for r in self.conn.execute("SELECT username,user_id FROM local_accounts"):
            named.setdefault(r["user_id"], r["username"])
        # The roster first: cheap columns only, so the pager can be answered
        # without paying for storage reads on accounts nobody is looking at.
        base = []
        for r in self.conn.execute("SELECT id,name,created_at FROM users"):
            who = r["id"]
            base.append({"user_id": who,
                         "username": named.get(who) or r["name"] or who,
                         "admin": who in admins,
                         "created_at": r["created_at"]})
        if want:
            base = [b for b in base
                    if want in (b["username"] or "").lower()
                    or want in (b["user_id"] or "").lower()]
        base.sort(key=lambda b: (not b["admin"], (b["username"] or "").lower()))
        total = len(base)
        page = base[offset:offset + limit]
        if not page:
            return 200, {"accounts": [], "you": uid, "total": total,
                         "offset": offset, "limit": limit, "filter": want,
                         "tiers": list(quota.TIERS),
                         "registration_open": auth.registration_open(self.conn)}

        subs = billing.list_all(self.conn)
        all_grants = grants.list_all(self.conn)
        books = {}
        for r in self.conn.execute(
                "SELECT user_id,COUNT(*) n FROM books GROUP BY user_id"):
            books[r["user_id"]] = int(r["n"] or 0)
        seen, sessions = {}, {}
        for s in auth.list_all_sessions(self.conn, q.get("_token") or ""):
            u = s["user"]
            sessions[u] = sessions.get(u, 0) + 1
            seen[u] = max(seen.get(u, 0), int(s.get("last_seen") or 0))
        rows = []
        for b in page:
            who = b["user_id"]
            sub = subs.get(who) or {}
            plan = sub.get("plan") or quota.FREE_PLAN
            blocks = billing.blocks(self.conn, who)
            g = all_grants.get(who) or {}
            use = stats.user_stats(self.conn, who)
            try:
                stor = quota.usage(self.conn, self.storage, who)["own_bytes"]
            except Exception:
                stor = 0
            rows.append({
                "user_id": who,
                "username": b["username"],
                "admin": b["admin"],
                "created_at": int(b["created_at"] or 0) if str(b["created_at"] or "").isdigit() else 0,
                "plan": plan,
                "plan_status": sub.get("status") or "",
                "plan_provider": sub.get("provider") or "",
                "storage_blocks": blocks,
                "storage_bytes": stor,
                "allowance_bytes": quota.allowance_bytes(plan, blocks),
                "storage_unlimited": bool(g.get("storage_unlimited")),
                "credits_unlimited": bool(g.get("credits_unlimited")),
                "balance": round(accounting.balance(self.conn, who), 2),
                "currency": accounting.currency(self.conn, who) or "USD",
                "books": books.get(who, 0),
                "chars_voiced": int(use.get("chars_total") or 0),
                "books_listened": int(use.get("books_listened") or 0),
                "listen_seconds": float(use.get("listen_seconds") or 0.0),
                "sessions": sessions.get(who, 0),
                "last_seen": seen.get(who, 0),
            })
        return 200, {"accounts": rows, "you": uid, "total": total,
                     "offset": offset, "limit": limit, "filter": want,
                     "tiers": list(quota.TIERS),
                     "registration_open": auth.registration_open(self.conn)}

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
                     "has_password": bool(uid) and auth.has_local_account(self.conn, uid),
                     # the account's theme: a new browser asks here, because the
                     # browser's own copy says nothing about a machine it has
                     # never seen.
                     "theme": connections.get_theme(self.conn, uid) if uid else ""}

    def auth_linked(self, q, body):
        """Which social providers the signed-in user has attached."""
        uid = (q or {}).get("_user") or ""
        if not uid:
            return 401, {"error": "auth required"}
        return 200, {"linked": auth.linked_providers(self.conn, uid)}

    # ---- the second factor ---------------------------------------------------
    # Six login paths mint sessions, and none of them mentions two-factor: the
    # gate lives in the dispatch table plus auth.create_session. These handlers
    # are the only places that know a factor exists.

    def _twofa_who(self, q):
        """(factor, user_id) for the caller, pending or not.

        A half-session has a user id but no authority, so every handler below
        reads both from here instead of from q["_user"], which is "" until the
        factor is done.
        """
        factor, uid = auth.session_state(self.conn, (q or {}).get("_token") or "")
        return (factor or ""), (uid or "")

    def _account_name(self, uid):
        """What the authenticator app should label this account as."""
        row = self.conn.execute("SELECT username FROM local_accounts WHERE user_id=?",
                               (uid,)).fetchone()
        return ((row["username"] if row else "") or uid)

    def _allow_attempt(self, uid, kind):
        return self.door_limiter.allow("2fa:%s:%s" % (kind, uid or "?"))

    def twofa_status(self, q, body):
        """What this session still owes, and what it may do about it.

        `required` means this account must carry a factor: an administrator
        always, everyone else when the deployment requires it. The panel says
        what is true rather than what is allowed, so "can_disable" is false
        exactly when a factor is not the account's own choice."""
        factor, uid = self._twofa_who(q)
        admin = bool(uid) and auth.is_admin(self.conn, uid)
        forced = auth.must_have_factor(self.conn, uid)
        # an admin who is the only account on the box is not held at an
        # enrolment wall (first run, own front door); the rule is deferred, not
        # dropped, and it applies again the moment a second account exists.
        deferred = forced and auth.bootstrap_skip(self.conn, uid)
        return 200, {
            "twofa": factor if factor in auth.PENDING else "",
            "enrolled": auth.enrolled(self.conn, uid),
            "recovery_left": auth.recovery_code_left(self.conn, uid),
            "required": forced,
            "deferred": bool(deferred) and not auth.enrolled(self.conn, uid),
            "admin": admin,
            "can_disable": bool(uid) and not forced,
        }

    def twofa_enroll(self, q, body):
        """Start enrolment: a fresh secret and the otpauth:// URI for the phone.

        Starting over is safe and deliberate: begin_enroll replaces the secret
        and un-confirms it, so an interrupted enrolment cannot leave a key
        behind that nobody scanned. That is exactly why replacing a CONFIRMED
        factor asks for the current one first. Without that proof a stolen
        session could move the second factor onto its own phone, which is the
        whole thing the factor exists to stop.
        """
        factor, uid = self._twofa_who(q)
        if not uid:
            return 401, {"error": "auth required"}
        if not self._allow_attempt(uid, "enroll"):
            return 429, {"error": "too many attempts"}
        b = body or {}
        changing = auth.enrolled(self.conn, uid)
        if changing:
            ok = auth.check_code(self.conn, uid, b.get("code"))
            if not ok and (b.get("recovery") or "").strip():
                ok = auth.spend_recovery_code(self.conn, uid, b.get("recovery"))
            if not ok:
                return 401, {"error": "enter a current code to change your "
                                      "authenticator", "needs_proof": True}
        account = b.get("account") or self._account_name(uid)
        out = auth.begin_enroll(self.conn, uid, account)
        out["changing"] = changing
        out["twofa"] = factor if factor in auth.PENDING else ""
        return 200, out

    def twofa_confirm(self, q, body):
        """Prove the secret works. Nothing may depend on it before this passes."""
        factor, uid = self._twofa_who(q)
        if not uid:
            return 401, {"error": "auth required"}
        if not self._allow_attempt(uid, "confirm"):
            return 429, {"error": "too many attempts"}
        codes = auth.confirm_enroll(self.conn, uid, (body or {}).get("code"))
        if codes is None:
            return 400, {"error": "that code did not match"}
        out = {"ok": True, "twofa": "", "recovery": codes,
               "recovery_left": len(codes)}
        if factor in auth.PENDING:
            # an admin finishing a forced enrolment is not made to log in a
            # second time: the same session becomes a full one here.
            auth.satisfy_factor(self.conn, (q or {}).get("_token") or "")
        return 200, out

    def _code_refusal(self, uid, code):
        """A refusal to return for a typed code, or None when it was good.

        "already been used" is its own sentence because the commonest replay is
        innocent: right after turning the factor on, the same digits are still
        on the phone. Saying "did not match" to that sends a person round the
        same loop with a code that was right.
        """
        st = auth.check_code_status(self.conn, uid, code)
        if st == "ok":
            return None
        if st == "replayed":
            return 401, {"error": "that code has already been used; wait for the "
                                  "next one, then type it"}
        return 401, {"error": "that code did not match"}

    def twofa_verify(self, q, body):
        """Finish a login: a TOTP code or a recovery code, then a full session."""
        factor, uid = self._twofa_who(q)
        if not uid:
            return 401, {"error": "auth required"}
        if factor not in auth.PENDING:
            return 200, {"ok": True, "twofa": ""}
        if not self._allow_attempt(uid, "verify"):
            return 429, {"error": "too many attempts"}
        b = body or {}
        used = "code"
        if (b.get("recovery") or "").strip():
            used = "recovery"
            if not auth.spend_recovery_code(self.conn, uid, b.get("recovery")):
                return 401, {"error": "that recovery code is not valid"}
        else:
            bad = self._code_refusal(uid, b.get("code"))
            if bad:
                return bad
        auth.satisfy_factor(self.conn, (q or {}).get("_token") or "")
        return 200, {"ok": True, "user": uid, "twofa": "", "used": used,
                     "recovery_left": auth.recovery_code_left(self.conn, uid)}

    def twofa_recovery(self, q, body):
        """New set of one-time codes, for an already enrolled account."""
        factor, uid = self._twofa_who(q)
        if not uid or factor in auth.PENDING or not auth.enrolled(self.conn, uid):
            return 401, {"error": "enrol two-factor first"}
        if not self._allow_attempt(uid, "recovery"):
            return 429, {"error": "too many attempts"}
        bad = self._code_refusal(uid, (body or {}).get("code"))
        if bad:
            return bad
        codes = auth.issue_recovery_codes(self.conn, uid)
        return 200, {"ok": True, "recovery": codes, "recovery_left": len(codes)}

    def twofa_disable(self, q, body):
        """Turn the factor off, given a current code.

        An administrator is always refused: an admin account carries a factor,
        full stop, and this is the route that would quietly undo that. Refusing
        is not a lockout, because there are three ways back in that keep the
        factor on: a recovery code finishes a login, changing the authenticator
        accepts a recovery code as proof, and tools/reset_2fa.py clears the
        factor from inside the container, after which the next login must enrol
        again. A non-admin is refused only when the deployment requires a factor
        of everyone.
        """
        factor, uid = self._twofa_who(q)
        if not uid or factor in auth.PENDING:
            return 401, {"error": "auth required"}
        if auth.must_have_factor(self.conn, uid):
            why = ("administrators must keep a second factor"
                   if auth.is_admin(self.conn, uid)
                   else "this server requires a second factor for every account")
            return 403, {"error": why + "; you can change it, or use a recovery "
                                       "code"}
        if not self._allow_attempt(uid, "disable"):
            return 429, {"error": "too many attempts"}
        b = body or {}
        bad = self._code_refusal(uid, b.get("code"))
        if bad and (b.get("recovery") or "").strip():
            if auth.spend_recovery_code(self.conn, uid, b.get("recovery")):
                bad = None
        if bad:
            return bad
        auth.disable_2fa(self.conn, uid)
        return 200, {"ok": True, "enrolled": False}

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
        elif _norm_confirm(body.get("confirm")) not in DELETE_PHRASES:
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

    def admin_account_delete(self, q, body):
        """Admin: erase somebody else's account.

        Approval to delete is not the same as taking the decision to delete, so
        the confirmation is the target's own name typed out, not a button. This
        never deletes the calling admin (the account page is the place for
        that, and it knows the password rules) and never deletes another admin:
        an admin removes admin rights where it leaves a trace, rather than by
        erasing the person.
        """
        uid = (q or {}).get("_user") or ""
        if not auth.is_admin(self.conn, uid):
            return 403, {"error": "admin only"}
        body = body or {}
        target = (body.get("user") or "").strip()
        if not target:
            return 400, {"error": "user required"}
        if target == uid:
            return 409, {"error": "that is your own account",
                         "detail": "delete your own account from Settings > Account"}
        if auth.is_admin(self.conn, target):
            return 409, {"error": "that account is an admin",
                         "detail": "remove the admin row first, then delete it"}
        row = self.conn.execute("SELECT id,name FROM users WHERE id=?",
                                (target,)).fetchone()
        if not row:
            return 404, {"error": "no such account"}
        typed = (body.get("confirm") or "").strip()
        expect = {target}
        for r in self.conn.execute("SELECT username FROM local_accounts WHERE user_id=?",
                                   (target,)):
            expect.add(r["username"])
        if row["name"]:
            expect.add(row["name"])
        if typed not in expect:
            return 400, {"error": "confirm",
                         "detail": "type the account's username to confirm"}
        billing_out = self._cancel_billing_for(target)
        out = erase.purge_user(self.conn, self.storage, target)
        auth.revoke_sessions(self.conn, target, "")
        if billing_out.get("subscription_id") and not billing_out["cancelled"]:
            self._claim_payment(
                "cancel-failed:%s:%s" % (target, db.now()), "cancel_failed",
                target, billing_out.get("subscription_id", ""), 0,
                billing_out.get("detail", ""))
        return 200, {"ok": True, "user": target, "books": out.get("books", 0),
                     "freed": out.get("freed", 0), "billing": billing_out}

GET_ROUTES = {
    "/": "ui", "/index.html": "ui", "/favicon.ico": "favicon", "/i18n.js": "i18n_js",
    "/hearth.mp4": "hearth", "/hearth-poster.jpg": "hearth_poster",
    "/loader.svg": "loader",
    "/manifest.webmanifest": "manifest", "/sw.js": "service_worker",
    "/icon-192.png": "icon192", "/icon-512.png": "icon512",
    "/icon-maskable-512.png": "icon_maskable",
    "/privacy": "privacy", "/terms": "terms", "/refunds": "refunds",
    "/delete-account": "delete_account",
    "/about": "about", "/contact": "contact", "/kokoro": "kokoro",
    "/pricing": "pricing", "/checkout": "checkout",
    "/health": "health", "/plugins": "plugins", "/library": "library_list",
    "/series": "series_list", "/tags": "tags_get",
    "/job": "job", "/jobs": "jobs_list", "/progress": "progress", "/chapter": "chapter",
    "/estimate": "estimate",
    "/position": "get_position", "/last": "last_position", "/stop": "stop", "/text": "text",
    "/stats": "stats_get", "/pron": "pron_get",
    "/pron/impact": "pron_impact", "/pron/dicts": "pron_dicts",
    "/connections": "connections_list", "/source-creds": "source_creds",
    "/cover": "cover_get",
    "/auth/providers": "auth_providers", "/me": "me", "/auth/policy": "auth_policy",
    "/auth/linked": "auth_linked",
    "/sessions": "sessions_list",
    "/2fa": "twofa_status",
    "/auth/users": "auth_users", "/credits": "credits",
    "/credits/history": "credits_history",
    "/grants": "grants_list",
    "/admin/plans": "plans_admin",
    "/admin/accounts": "admin_accounts",
    "/admin/audit": "admin_audit",
    "/admin/billing": "admin_billing",
    "/billing": "billing_get",
    "/notifications": "notices_list",
    # A GET: the page asks for its price ids on load, before anyone clicks.
    "/billing/config": "billing_config",
    "/plans": "plans_get",
    "/storage": "storage_get",
    "/disambig/models": "disambig_models",
    "/book/model": "book_model_get",
}
POST_ROUTES = {
    "/import": "import_book", "/import/preview": "preview_import",
    "/import/series": "import_series", "/book/example": "add_example_book",
    "/render": "render", "/voices": "voices",
    "/book/renditions/delete": "remove_renditions", "/book/delete": "delete_book",
    "/books/delete": "delete_books",
    "/account/delete": "account_delete",
    "/account/password": "account_password",
    "/speak": "speak",
    "/cover": "put_cover", "/cover/auto": "cover_auto", "/cover/clear": "clear_cover",
    "/book/details": "fetch_details", "/book/meta": "book_meta_set",
    "/book/series": "book_series_set", "/book/read": "book_read_set",
    "/series": "series_save", "/series/move": "series_move",
    "/tags": "tags_set",
    "/position": "put_position", "/claim": "claim", "/ack": "ack",
    "/listen": "listen", "/cancel": "cancel_job",
    "/credential": "put_credential", "/auth/registration": "set_registration",
    "/connections": "connection_save", "/connections/delete": "connection_delete",
    "/source-creds": "source_cred_save", "/source-creds/delete": "source_cred_delete",
    "/connections/override": "connection_override", "/prefs": "prefs_set",
    "/pron": "pron_set", "/pron/dicts": "pron_dict_save",
    "/disambig/scan": "disambig_scan", "/disambig/quote": "disambig_quote",
    "/book/model": "book_model_set",
    "/pron/dicts/delete": "pron_dict_delete",
    "/credits/purchase": "credits_purchase",
    "/grants": "grants_set",
    "/admin/plan": "plans_admin_set",
    "/admin/credit": "admin_credit",
    "/admin/account/delete": "admin_account_delete",
    "/admin/billing": "admin_billing_save",
    "/admin/billing/check": "admin_billing_check",
    "/billing/checkout": "billing_checkout", "/billing/confirm": "billing_confirm",
    "/billing/cancel": "billing_cancel", "/billing/portal": "billing_portal",
    "/billing/change": "billing_change", "/billing/resume": "billing_resume",
    "/billing/storage": "billing_storage",
    "/notifications/read": "notices_read",
    "/credits/confirm": "credits_confirm",
    "/2fa/enroll": "twofa_enroll", "/2fa/confirm": "twofa_confirm",
    "/2fa/verify": "twofa_verify", "/2fa/recovery": "twofa_recovery",
    "/2fa/disable": "twofa_disable",
    "/sessions/revoke": "session_revoke",
    "/sessions/revoke_all": "sessions_revoke_all",
}

def signature_header(headers, provider_id="stripe"):
    """The signature header a provider actually sends.

    Stripe sends Stripe-Signature and Paddle sends Paddle-Signature, so reading
    one literal name meant every Paddle delivery arrived "unsigned" and was
    rejected with a 400 on the first real sale. The name is derived from the
    provider id in the path and both spellings are accepted, because a proxied
    or renamed provider must not be able to silently drop the signature.
    """
    headers = headers or {}
    pid = (provider_id or "stripe").strip().lower()
    for name in ("%s-Signature" % pid.capitalize(), "Paddle-Signature",
                 "Stripe-Signature"):
        value = headers.get(name, "")
        if value:
            return value
    return ""


# reachable without a session even when auth is required: the page, its health,
# the sign-in surface, and the browser's cache of who it is.
PUBLIC_ROUTES = {"/", "/index.html", "/i18n.js", "/favicon.ico", "/health", "/auth/providers",
                 "/auth/policy", "/auth/forgot", "/auth/reset", "/me", "/plans",
                 "/privacy", "/terms", "/refunds", "/delete-account", "/pricing",
                 "/checkout",
                 "/about", "/contact", "/kokoro",
                 # The door's fire sits behind a signed-out visitor's first
                 # screen, so it has to load before there is a session.
                 "/hearth.mp4", "/hearth-poster.jpg", "/loader.svg",
                 # A signed-out visitor sees prices before signing in, so the
                 # overlay's price ids have to be readable without a session.
                 "/billing/config",
                 "/manifest.webmanifest", "/sw.js",
                 # The page asks /2fa on every load, before it can know whether
                 # a session exists. Answering 401 there put a console error on
                 # a signed-out visitor's page and on the very screen that is
                 # asking them to sign in. With no session the honest answer is
                 # an empty state, the same shape /me gives, so it is public.
                 "/2fa",
                 "/icon-192.png", "/icon-512.png", "/icon-maskable-512.png"}

# Largest request body a route will read. A story or a PDF arrives base64 in a
# JSON body, so the general cap is generous; the doors that need no session
# (sign-in, reset, webhooks) carry a few short fields and get a small one.
BODY_MAX_BYTES = 96 * 1024 * 1024
DOOR_MAX_BYTES = 1024 * 1024


# What a half-session may still reach: the screen that finishes the factor,
# and nothing else. Keyed by the factor it owes, because an admin who must
# enrol and a user who must type a code need different doors.
PENDING_ROUTES = {
    auth.PENDING_CODE: {"/2fa", "/2fa/verify"},
    auth.PENDING_ENROLL: {"/2fa", "/2fa/enroll", "/2fa/confirm"},
}

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


# A pass is a few minutes on a big book. A "running" row older than this was a
# thread that died (a restart, an OOM), and leaving it forever would mean the
# button never works again for that book.
_PASS_CLAIM_SECONDS = 30 * 60


def _stale_claim(stamp):
    if not stamp:
        return True
    try:
        when = time.mktime(time.strptime(str(stamp)[:19], "%Y-%m-%dT%H:%M:%S"))
    except Exception:
        return True
    return (time.time() - when) > _PASS_CLAIM_SECONDS


# The cookies that carry an OAuth round trip, by the name a caller uses for them.
_OAUTH_COOKIES = {"state": auth.STATE_COOKIE, "link": auth.LINK_COOKIE,
                  "claim": auth.CLAIM_COOKIE, "app": auth.APP_COOKIE,
                  "pkce": auth.PKCE_COOKIE}


def _int_env(name, default):
    try:
        return max(0, int(os.environ.get(name, "")))
    except ValueError:
        return default


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

        def _log_fault(self):
            """Print the traceback with a short reference and return the reference.

            The client gets the reference and nothing else: an exception message
            can carry SQL, a hostname or a path. An operator matches the two.
            """
            import traceback
            ref = os.urandom(4).hex()
            print("fault %s %s %s" % (ref, self.command, self.path),
                  file=sys.stderr)
            traceback.print_exc()
            return ref

        def _read_body(self, max_bytes):
            """(raw, None), or (None, (status, message)) when the body is refused.

            The length comes from the client, so it is checked before anything is
            read: a bad or negative value is a 400 and an oversize one a 413,
            instead of a thread blocked on rfile.read(-1) or a book-sized buffer
            from someone who never signed in.
            """
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if n < 0:
                return None, (400, "bad Content-Length")
            if n > max_bytes:
                return None, (413, "request body too large")
            return (self.rfile.read(n) if n else b""), None

        def _read_json(self, max_bytes):
            """(object, None), or (None, (status, message)). An empty body is {}."""
            raw, err = self._read_body(max_bytes)
            if err:
                return None, err
            if not raw:
                return {}, None
            try:
                body = json.loads(raw.decode("utf-8"))
            except ValueError:
                return None, (400, "request body is not valid JSON")
            if not isinstance(body, dict):
                return None, (400, "request body must be a JSON object")
            return body, None

        def _new_session(self, uid, kind):
            return auth.create_session(app.conn, uid, kind,
                                       ua=self._ua(), ip=self._ip())

        def _session_cookie(self, token):
            return self._cookie_str(auth.SESSION_COOKIE, token,
                                    max_age=auth.SESSION_TTL)

        def _oauth_cookies(self, **values):
            """Set the short-lived sign-in cookies named in `values`, or clear all.

            One list of names serves both: the cookies a start sets and the ones
            a callback clears cannot drift apart, and a cookie added to the flow
            is cleared with the rest.
            """
            if not values:
                return [self._cookie_str(c, "", path="/auth", max_age=0)
                        for c in _OAUTH_COOKIES.values()]
            return [self._cookie_str(_OAUTH_COOKIES[k], v, path="/auth", max_age=600)
                    for k, v in values.items()]

        def _ua(self):
            return (self.headers.get("User-Agent") or "")[:400]

        def _ip(self):
            """The caller's address, for rate limits and the session list.

            Default: the socket peer, which a client cannot forge. Behind a proxy
            that is the proxy for everyone, so every caller would share one
            sign-in budget. FIRESIDE_TRUSTED_PROXY_HOPS=N says N proxies of ours
            append to X-Forwarded-For; the Nth entry from the right is then the
            address our outermost proxy saw. Counting from the right matters: the
            left of the header is whatever the client chose to send.
            """
            peer = (self.client_address[0] if self.client_address else "")
            hops = _int_env("FIRESIDE_TRUSTED_PROXY_HOPS", 0)
            if hops > 0:
                chain = [p.strip() for p in
                         (self.headers.get("X-Forwarded-For") or "").split(",")
                         if p.strip()]
                if len(chain) >= hops:
                    return chain[-hops][:64]
            return peer[:64]

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
                    return self._redirect(url, self._oauth_cookies(
                        state=state, link=uid if link else "", claim=claim,
                        app="1" if for_app else "", pkce=verifier))
                if action == "callback":
                    if q.get("error"):
                        return self.send_error(400, "oauth error: %s" % q["error"])
                    if not q.get("state") or q["state"] != self._cookie(auth.STATE_COOKIE):
                        return self.send_error(400, "bad oauth state")
                    for_app = bool(self._cookie(auth.APP_COOKIE))
                    verifier = self._cookie(auth.PKCE_COOKIE)
                    clear = self._oauth_cookies()
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
                        token = self._new_session(uid, "link")
                        return self._redirect("/", clear + [self._session_cookie(token)])
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
                    return self._redirect("/", clear + [self._session_cookie(token)])
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
                tok = self._new_session(uid, "app")
                return self._redirect("/", [self._session_cookie(tok)])
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
                body, err = self._read_json(DOOR_MAX_BYTES)
                if err:
                    return self._json(err[0], {"error": err[1]})
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
                token = self._new_session(uid, "local")
                return self._json(200, {"ok": True, "user": uid,
                                        "admin": auth.is_admin(app.conn, uid)},
                                  cookie=self._session_cookie(token))
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
                body, err = self._read_json(DOOR_MAX_BYTES)
                if err:
                    return self._json(err[0], {"error": err[1]})
                try:
                    uid = auth.complete_claim(app.conn, body.get("claim", ""),
                                              body.get("password", ""))
                except auth.AuthError as e:
                    return self._json(401, {"error": str(e)})
                token = self._new_session(uid, "link")
                return self._json(200, {"ok": True, "user": uid},
                                  cookie=self._session_cookie(token))
            # Forgot password: ask for an address, mail a one-time link. The
            # answer is the same whether or not an account matched - telling the
            # two apart would turn this door into "who has an account here?".
            if path == "/auth/forgot" and self.command == "POST":
                # Sends mail: unmetered, it is a free way to make us a spam relay
                # and to enumerate who has an account here.
                if not app.door_limiter.allow("forgot:" + (self._ip() or "?")):
                    return self._json(429, {"error": "too many attempts"})
                from urllib.parse import quote
                body, err = self._read_json(DOOR_MAX_BYTES)
                if err:
                    return self._json(err[0], {"error": err[1]})
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
                body, err = self._read_json(DOOR_MAX_BYTES)
                if err:
                    return self._json(err[0], {"error": err[1]})
                try:
                    uid = auth.reset_password(app.conn, body.get("token", ""),
                                              body.get("password", ""))
                except auth.AuthError as e:
                    return self._json(401, {"error": str(e)})
                token = self._new_session(uid, "reset")
                return self._json(200, {"ok": True, "user": uid},
                                  cookie=self._session_cookie(token))
            # Stripe's server-to-server webhook. Public by necessity: Stripe
            # holds no session, so the signature is the authentication. It is
            # read raw here, before any JSON parse, because the signature is
            # computed over the exact bytes. No session, no auth gate.
            if path.startswith("/webhook/") and self.command == "POST":
                payload, err = self._read_body(DOOR_MAX_BYTES)
                if err:
                    return self._json(err[0], {"error": err[1]})
                pid = parts[1] if len(parts) > 1 else "stripe"
                sig = signature_header(self.headers, pid)
                status, body = app.payment_webhook(payload, sig, pid)
                return self._json(status, body)
            name = routes.get(path)
            if name is None:
                return self.send_error(404)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            body = {}
            if self.command == "POST":
                body, err = self._read_json(BODY_MAX_BYTES)
                if err:
                    return self._json(err[0], {"error": err[1]})
            # The session lookup sits outside the per-handler try on purpose: it
            # runs before routing, so it has no handler to hand an error to. But
            # an unguarded raise here closes the connection with no status and no
            # traceback, which reads to a client as "logged out" and to an
            # operator as nothing at all. A lookup that fails is a server fault,
            # so say 503 and log it.
            try:
                q["_user"] = auth.session_user(app.conn, self._token()) or ""
            except Exception:
                return self._json(503, {"error": "session lookup failed",
                                        "ref": self._log_fault()})
            q["_token"] = self._token()
            # ---- the second factor gate, once ------------------------------
            # A half-session authorises nothing except finishing the factor.
            # Every login path mints through auth.create_session, so this is
            # the one place enforcement has to exist: a new door added later
            # inherits it without knowing 2FA exists.
            try:
                q["_pending"] = auth.pending_state(app.conn, q["_token"]) or ""
            except Exception:
                q["_pending"] = ""
            q["_allowed"] = (PENDING_ROUTES.get(q["_pending"], frozenset())
                             if q["_pending"] else frozenset())
            if q["_pending"]:
                public = path in PUBLIC_ROUTES and path != "/me"
                if path not in q["_allowed"] and not public:
                    return self._json(401, {"twofa": q["_pending"],
                                            "error": "second factor required"})
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
            if (app.require_auth and path not in PUBLIC_ROUTES and not q["_user"]
                    and path not in q["_allowed"]):
                return self._json(401, {"error": "auth required"})
            try:
                status, payload = getattr(app, name)(q, body)
            except Exception:
                return self._json(500, {"error": "internal error",
                                        "ref": self._log_fault()})
            if isinstance(payload, dict) and "_html" in payload:
                page = payload["_html"]
                if payload.get("_locale"):
                    page = i18n.inject(page, code=q.get("_lang"),
                                       theme=payload.get("_theme"))
                raw = page.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            elif isinstance(payload, dict) and "_raw" in payload:
                raw = payload["_raw"]
                # Media asks for a slice; a body shorter than the file it asked
                # for is watched as a broken clip, not as a fast stream.
                span = _byte_range(self.headers.get("Range"), len(raw))
                if span:
                    start, end = span
                    self.send_response(206)
                    self.send_header("Content-Range",
                                     "bytes %d-%d/%d" % (start, end, len(raw)))
                    body = raw[start:end + 1]
                else:
                    self.send_response(status)
                    body = raw
                self.send_header("Content-Type", payload.get("_mime", "application/octet-stream"))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(len(body)))
                if payload.get("_cache"):
                    self.send_header("Cache-Control", payload["_cache"])
                self.send_header("X-Checksum", payload.get("checksum", ""))
                self.end_headers()
                self.wfile.write(body)
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

        def log_request(self, code="-", size="-"):
            """One line per request on stderr: who, what, how it went.

            The query string is dropped before anything is printed. Several URLs
            here carry a live capability in it (/auth/claim?token=,
            /auth/app/session?token=, /?reset=), and a log is where those leak.
            FIRESIDE_ACCESS_LOG=0 turns the line off.
            """
            if (os.environ.get("FIRESIDE_ACCESS_LOG") or "1").strip() in ("0", "false", "no"):
                return
            path = (self.path or "").split("?", 1)[0].split("#", 1)[0][:200]
            print("%s %s %s %s" % (self._ip() or "-", self.command, path,
                                   getattr(code, "value", code)), file=sys.stderr)

        def log_message(self, *args):
            pass        # protocol-level noise; log_request above is the access log

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
    # The app applies the settings saved in the DB into the environment, so it
    # has to exist before the provider is registered; otherwise a key saved on
    # the admin screen is invisible until the next deploy.
    app = app or App()
    payments.load()      # register the payment provider named by FIRESIDE_PAYMENT_PROVIDER
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


