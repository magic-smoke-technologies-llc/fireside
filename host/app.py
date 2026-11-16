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

import inspect
import json
import os
import sys
import time
import urllib.parse
from host import allowance, auth, connections, db, disambig, i18n, jobs, keepwarm, library, mailer, paymentapply, payments, ratelimit, settings
from host.host import Host
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from host.appsupport import (
    ASSETS_DIR,
    BASE_DIR,
    DELETE_PHRASES,
    HOST_VERSION,
    UI_DIR,
    _PASS_CLAIM_SECONDS,
    _byte_range,
    _detail,
    _int_env,
    _ledger_row,
    _norm_confirm,
    _sale_connection_from_env,
    _sale_offer,
    _stale_claim,
    _swallowed,
    _truthy,
    _with_idem)
from host.routes_disambig import DisambigRoutes
from host.routes_static import StaticRoutes
from host.routes_library import LibraryRoutes
from host.routes_render import RenderRoutes
from host.routes_billing import BillingRoutes
from host.routes_jobs import JobRoutes
from host.routes_voice import VoiceRoutes
from host.routes_admin import AdminRoutes
from host.routes_account import AccountRoutes

class App(DisambigRoutes, StaticRoutes, LibraryRoutes, RenderRoutes, BillingRoutes, JobRoutes, VoiceRoutes, AdminRoutes, AccountRoutes):
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


# The cookies that carry an OAuth round trip, by the name a caller uses for them.
_OAUTH_COOKIES = {"state": auth.STATE_COOKIE, "link": auth.LINK_COOKIE,
                  "claim": auth.CLAIM_COOKIE, "app": auth.APP_COOKIE,
                  "pkce": auth.PKCE_COOKIE}


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

            Also called for a request line that never parsed (too long, bad
            version), before path or headers exist, so nothing here may assume
            them, and a logging fault must never cost the response.
            """
            try:
                off = (os.environ.get("FIRESIDE_ACCESS_LOG") or "1").strip()
                if off in ("0", "false", "no"):
                    return
                path = getattr(self, "path", "") or ""
                path = path.split("?", 1)[0].split("#", 1)[0][:200]
                if getattr(self, "headers", None) is not None:
                    ip = self._ip()
                else:
                    ip = self.client_address[0] if self.client_address else ""
                print("%s %s %s %s" % (ip or "-", getattr(self, "command", "") or "-",
                                       path or "-", getattr(code, "value", code)),
                      file=sys.stderr)
            except Exception:
                pass

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


