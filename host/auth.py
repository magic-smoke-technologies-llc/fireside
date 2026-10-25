"""Social sign-in for the host: OAuth authorization-code flow + server sessions.

One deployment-agnostic shape: the host owns the session, the provider owns the
identity. No plugin is involved, no provider is imported by name - the table
below is data. A provider only exists if its client id and secret are present in
the environment, so a deployment without credentials simply offers no buttons,
and no secret ever lives in the repo.

Contract:
  providers()                     -> [{id, label, start}] for the configured set
  authorize_url(pid, redirect, s) -> the URL to send the browser to
  login(pid, code, redirect)      -> (user_id, session_token) after a callback
  session_user(token)             -> user_id or None (expiry enforced)
  list_sessions(user_id, tok)     -> a user's named live sessions, current flagged
  revoke_session(user_id, sid)    -> log out one session by its public id
  revoke_sessions(user_id, keep)  -> log out the rest, or all when keep is empty
"""

import base64
import hashlib
import json
import os
import re
import secrets
import time
import urllib.parse
import urllib.request

SESSION_TTL = 30 * 24 * 3600          # 30 days; cookie and row share it
STATE_COOKIE = "fireside_oauth_state"
LINK_COOKIE = "fireside_oauth_link"
CLAIM_COOKIE = "fireside_oauth_claim"
SESSION_COOKIE = "fireside_session"

# provider table: data, not code paths. id_env names the two secrets to read.
PROVIDERS = {
    "github": {
        "label": "GitHub",
        "authorize": "https://github.com/login/oauth/authorize",
        "token": "https://github.com/login/oauth/access_token",
        "identity": "https://api.github.com/user",
        "emails": "https://api.github.com/user/emails",
        "scope": "read:user user:email",
        "id_env": ("FIRESIDE_OAUTH_GITHUB_CLIENT_ID", "FIRESIDE_OAUTH_GITHUB_SECRET"),
    },
    "google": {
        "label": "Google",
        "authorize": "https://accounts.google.com/o/oauth2/v2/auth",
        "token": "https://oauth2.googleapis.com/token",
        "identity": "https://openidconnect.googleapis.com/v1/userinfo",
        "scope": "openid email profile",
        "id_env": ("FIRESIDE_OAUTH_GOOGLE_CLIENT_ID", "FIRESIDE_OAUTH_GOOGLE_SECRET"),
    },
    "microsoft": {
        "label": "Microsoft",
        "authorize": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "token": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "identity": "https://graph.microsoft.com/oidc/userinfo",
        "scope": "openid email profile",
        "id_env": ("FIRESIDE_OAUTH_MICROSOFT_CLIENT_ID", "FIRESIDE_OAUTH_MICROSOFT_SECRET"),
    },
    "facebook": {
        "label": "Facebook",
        "authorize": "https://www.facebook.com/v21.0/dialog/oauth",
        "token": "https://graph.facebook.com/v21.0/oauth/access_token",
        "identity": "https://graph.facebook.com/me?fields=id,name,email",
        "scope": "email public_profile",
        "id_env": ("FIRESIDE_OAUTH_FACEBOOK_CLIENT_ID", "FIRESIDE_OAUTH_FACEBOOK_SECRET"),
    },
    # X is OAuth 2.0 with PKCE, and it does NOT hand back an email address. An X
    # account therefore proves an identity and nothing else: the login works,
    # and the account has no address to be found by. That is the honest shape of
    # it, so the login door makes no email-shaped promise here.
    "x": {
        "label": "X",
        "authorize": "https://x.com/i/oauth2/authorize",
        "token": "https://api.x.com/2/oauth2/token",
        "identity": "https://api.x.com/2/users/me",
        "scope": "users.read",
        "pkce": True,
        "id_env": ("FIRESIDE_OAUTH_X_CLIENT_ID", "FIRESIDE_OAUTH_X_SECRET"),
    },
    # Proton has no consumer OAuth of its own; SimpleLogin (Proton-owned) is the
    # door. Its sign-in page carries a "Log in with Proton" button, and it hands
    # back a verified email. SimpleLogin documents the 'profile' scope only.
    "simplelogin": {
        "label": "Proton (SimpleLogin)",
        "authorize": "https://app.simplelogin.io/oauth2/authorize",
        "token": "https://app.simplelogin.io/oauth2/token",
        "identity": "https://app.simplelogin.io/oauth2/userinfo",
        "scope": "profile",
        "id_env": ("FIRESIDE_OAUTH_SIMPLELOGIN_CLIENT_ID", "FIRESIDE_OAUTH_SIMPLELOGIN_SECRET"),
    },
}


PKCE_COOKIE = "fireside_oauth_pkce"


def _b64url(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def new_pkce():
    """A verifier and its S256 challenge, for the providers that demand PKCE."""
    verifier = secrets.token_urlsafe(64)[:96]
    return verifier, _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def _truthy(v):
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def _allow_local():
    """Local username/password accounts. On unless explicitly disabled."""
    v = os.environ.get("FIRESIDE_ALLOW_LOCAL")
    return True if v is None else _truthy(v)


def _digest(password, salt):
    return hashlib.pbkdf2_hmac("sha256", (password or "").encode("utf-8"),
                               bytes.fromhex(salt), 200000).hex()


def normalize_username(username):
    return (username or "").strip().lower()


def account_count(conn):
    """How many local accounts exist. The first one to arrive becomes admin."""
    return conn.execute("SELECT COUNT(*) c FROM local_accounts").fetchone()["c"]


def is_admin(conn, user_id):
    if not user_id:
        return False
    return conn.execute("SELECT 1 FROM admins WHERE user_id=?",
                        (user_id,)).fetchone() is not None


def admin_count(conn):
    """How many admins the server has. The last one must not delete itself:
    that leaves nobody who can administer the deployment."""
    row = conn.execute("SELECT COUNT(*) AS n FROM admins").fetchone()
    return int(row["n"] or 0)


def registration_open(conn):
    """Can a stranger still make an account by themselves?

    No setting row means yes: the default is that a fresh server is claimable,
    and an admin closes the door once the household is set up. A closed door
    never applies to the very first account.
    """
    row = conn.execute("SELECT value FROM settings WHERE `key`='registration'").fetchone()
    if row is None:
        return True
    return str(row["value"]).strip().lower() in ("open", "1", "true", "yes", "on")


def set_registration(conn, open_):
    v = "open" if open_ else "closed"
    conn.execute("DELETE FROM settings WHERE `key`='registration'")
    conn.execute("INSERT INTO settings(`key`,value) VALUES('registration',?)", (v,))
    conn.commit()
    return v


def local_accounts(conn):
    rows = conn.execute("SELECT username,user_id,created_at FROM local_accounts"
                        " ORDER BY created_at").fetchall()
    admins = {r["user_id"] for r in
              conn.execute("SELECT user_id FROM admins").fetchall()}
    return [{"username": r["username"], "user_id": r["user_id"],
             "created_at": r["created_at"], "admin": r["user_id"] in admins}
            for r in rows]


def _start_on_free_tier(conn, uid):
    """Give a freshly created account the free tier, once.

    A new sign-up starts at 500 MB (quota.FREE_ALLOWANCE_BYTES). An account that
    already has a subscription row is untouched, and an account with no row at
    all still fails open to the platform allowance - so accounts created before
    the free tier existed are not silently cut down.
    """
    conn.execute(
        "INSERT OR IGNORE INTO subscriptions"
        "(user_id,plan,status,provider,reference,current_period_end,updated_at,"
        "created_at) VALUES(?,?,?,?,?,?,?,?)",
        (uid, "free", "active", "", "", 0, _now(), _now()))


def local_register(conn, username, password, email="", require_email=False):
    """Create a local account. Raises AuthError if the name is taken or weak.

    `email` is optional for callers that predate it, but a server that wants the
    social door to spot an existing account should collect it: a password
    account's address is the only join key a social sign-in has. The address is
    stored unverified - nothing here proves the registrant can read that inbox -
    so it may only ever route a social login to the prove-it door, never merge.

    The first account on a fresh server is always allowed and becomes admin;
    every later account needs registration to be open.

    `require_email` closes the last way to make an unemailed account. A hosted
    server sets it: the address is the only key that lets a later social login
    find this account, and an account without one is invisible to that door -
    the same person logging in with Google silently gets a SECOND account, and
    nothing errors. Self-hosted single-user servers may keep it off, which is
    why the default is off rather than the reverse.
    """
    name = normalize_username(username)
    if len(name) < 3 or len(name) > 64:
        raise AuthError("username must be 3-64 characters")
    if not re.match(r"^[a-z0-9._-]+$", name):
        raise AuthError("username may use letters, digits, dot, dash, underscore")
    email = (email or "").strip().lower()
    if require_email and not email:
        raise AuthError("an email address is required")
    if email and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise AuthError("that email address does not look right")
    if len(email) > 255:
        raise AuthError("that email address is too long")
    if len(password or "") < 8:
        raise AuthError("password must be at least 8 characters")
    first = account_count(conn) == 0
    if not first and not registration_open(conn):
        raise AuthError("registration is closed on this server")
    if conn.execute("SELECT 1 FROM local_accounts WHERE username=?", (name,)).fetchone():
        raise AuthError("username already taken")
    uid = "local:%s" % name
    salt = secrets.token_hex(16)
    conn.execute("INSERT OR IGNORE INTO users(id,name,created_at) VALUES(?,?,?)",
                 (uid, name, _now()))
    _start_on_free_tier(conn, uid)
    conn.execute("INSERT INTO local_accounts(username,user_id,digest,salt,email,created_at)"
                 " VALUES(?,?,?,?,?,?)",
                 (name, uid, _digest(password, salt), salt, email, _now()))
    if first:
        conn.execute("INSERT OR IGNORE INTO admins(user_id,created_at) VALUES(?,?)",
                     (uid, _now()))
    conn.commit()
    return uid


def local_login(conn, username, password):
    """Verify a local account. Raises AuthError without saying which half failed."""
    name = normalize_username(username)
    row = conn.execute("SELECT user_id,digest,salt FROM local_accounts WHERE username=?",
                       (name,)).fetchone()
    if row is None:
        _digest(password or "", secrets.token_hex(16))   # same work, no leak
        raise AuthError("invalid username or password")
    if not secrets.compare_digest(_digest(password or "", row["salt"]), row["digest"]):
        raise AuthError("invalid username or password")
    return row["user_id"]


class AuthError(Exception):
    pass


class AccountExistsError(AuthError):
    """A new social identity collided with an existing account's verified email.

    Carries what the caller needs to ask for proof: the account already in the
    way, the provider, and the pending identity (linked once ownership is
    proven). A verified email is not proof of owning the account.
    """

    def __init__(self, user_id, provider, ident):
        super().__init__("an account with this email already exists")
        self.user_id = user_id
        self.provider = provider
        self.ident = ident


def _default_fetch(req, timeout=20):
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def new_state():
    return secrets.token_urlsafe(16)


def _identity(pid, data):
    """Normalize a provider's profile to {subject, name, email, email_verified}.

    email_verified is only ever trusted as the provider reports it; linking by
    email depends on it, so an unverified address links nothing.
    """
    verified = bool(data.get("email_verified"))
    if pid == "x":
        # {"data": {"id","name","username"}}, no email by design
        d = data.get("data") or {}
        return {"subject": str(d.get("id") or ""),
                "name": d.get("name") or d.get("username") or "",
                "email": "",
                "email_verified": False}
    if pid == "facebook":
        # Facebook returns an address only when the account has one and the
        # permission was granted; an address it does return is confirmed.
        email = (data.get("email") or "").strip()
        return {"subject": str(data.get("id") or ""),
                "name": data.get("name") or "",
                "email": email,
                "email_verified": bool(email)}
    if pid == "github":
        return {"subject": str(data.get("id") or ""),
                "name": data.get("name") or data.get("login") or "",
                "email": data.get("email") or "",
                "email_verified": verified}
    return {"subject": str(data.get("sub") or data.get("id") or ""),
            "name": data.get("name") or data.get("preferred_username") or "",
            "email": data.get("email") or "",
            "email_verified": verified}


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---- sessions (rows in sqlite; the cookie only carries the token) -------------

def _touch_after():
    return 300            # refresh last_seen at most this often, per token


def device_label(user_agent):
    """A short human name for a device, from its User-Agent string.

    Best-effort, never trusted for security: it only decides what the sessions
    list says beside a row. An empty UA (curl, an old client) reads as such.
    """
    ua = user_agent or ""
    low = ua.lower()
    if not ua.strip():
        return "Unknown device"
    browser = ""
    for token, name in (("edg/", "Edge"), ("opr/", "Opera"), ("firefox/", "Firefox"),
                        ("crios/", "Chrome"), ("chrome/", "Chrome"),
                        ("safari/", "Safari")):
        if token in low:
            browser = name
            break
    if not browser:
        browser = "Browser"
    kind = ""
    if "android" in low:
        kind = "Android"
    elif "iphone" in low or "ipad" in low or "ipod" in low:
        kind = "iOS"
    elif "windows" in low:
        kind = "Windows"
    elif "mac os x" in low or "macintosh" in low:
        kind = "macOS"
    elif "linux" in low:
        kind = "Linux"
    return "%s on %s" % (browser, kind) if kind else browser


def create_session(conn, user_id, provider, ttl=SESSION_TTL, ua="", ip=""):
    tok = secrets.token_urlsafe(32)
    sid = secrets.token_hex(8)          # a public handle, safe to show and revoke by
    now = int(time.time())
    conn.execute("INSERT INTO sessions"
                 "(token,user_id,provider,created_at,expires_at,sid,user_agent,ip,last_seen)"
                 " VALUES(?,?,?,?,?,?,?,?,?)",
                 (tok, user_id, provider, now, now + ttl, sid, ua or "", ip or "", now))
    conn.commit()
    return tok


def session_row(conn, token):
    """The live session row for a token, or None. Expiry deletes it; last_seen
    is refreshed but not on every request."""
    if not token:
        return None
    row = conn.execute("SELECT * FROM sessions WHERE token=?", (token,)).fetchone()
    if row is None:
        return None
    now = int(time.time())
    if int(row["expires_at"] or 0) < now:
        conn.execute("DELETE FROM sessions WHERE token=?", (token,))
        conn.commit()
        return None
    if now - int(row["last_seen"] or 0) > _touch_after():
        conn.execute("UPDATE sessions SET last_seen=? WHERE token=?", (now, token))
        conn.commit()
    return row


def session_user(conn, token):
    row = session_row(conn, token)
    return row["user_id"] if row is not None else None


def delete_session(conn, token):
    if token:
        conn.execute("DELETE FROM sessions WHERE token=?", (token,))
        conn.commit()


def list_sessions(conn, user_id, current_token=""):
    """A user's live sessions, newest activity first, each named by its device.

    The token never leaves the server; a row is addressed by its `sid`, and the
    one the caller is using now is flagged so the UI can say 'this device'.
    """
    if not user_id:
        return []
    now = int(time.time())
    rows = conn.execute(
        "SELECT token,sid,provider,created_at,expires_at,user_agent,ip,last_seen"
        " FROM sessions WHERE user_id=?"
        " ORDER BY last_seen DESC, created_at DESC", (user_id,)).fetchall()
    out = []
    for r in rows:
        if int(r["expires_at"] or 0) < now:
            continue
        out.append({
            "id": r["sid"] or "",
            "provider": r["provider"] or "",
            "label": device_label(r["user_agent"]),
            "user_agent": r["user_agent"] or "",
            "ip": r["ip"] or "",
            "created_at": int(r["created_at"] or 0),
            "last_seen": int(r["last_seen"] or 0),
            "current": bool(current_token) and r["token"] == current_token,
        })
    return out


def revoke_session(conn, user_id, sid):
    """Log out ONE session of `user_id`, named by its public id. Scoped by user
    so one account can never revoke another's. Returns True if a row went."""
    if not user_id or not sid:
        return False
    cur = conn.execute("DELETE FROM sessions WHERE user_id=? AND sid=?", (user_id, sid))
    conn.commit()
    return cur.rowcount > 0


def revoke_sessions(conn, user_id, keep_token=""):
    """Log out a user's sessions. Keep the one token when given (log out
    everywhere else); with no token, every session of the user goes.
    Returns how many were removed."""
    if not user_id:
        return 0
    cur = conn.execute("DELETE FROM sessions WHERE user_id=? AND token<>?",
                       (user_id, keep_token or "")) if keep_token else \
        conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
    conn.commit()
    return cur.rowcount


def list_all_sessions(conn, current_token=""):
    """Every live session, across every account, newest activity first.

    The admin view: each row carries the `user` it belongs to, so an admin can
    see the whole server and end any of them. The token still never leaves.
    """
    now = int(time.time())
    rows = conn.execute(
        "SELECT token,sid,user_id,provider,created_at,expires_at,user_agent,ip,last_seen"
        " FROM sessions ORDER BY last_seen DESC, created_at DESC").fetchall()
    out = []
    for r in rows:
        if int(r["expires_at"] or 0) < now:
            continue
        out.append({
            "id": r["sid"] or "",
            "user": r["user_id"] or "",
            "provider": r["provider"] or "",
            "label": device_label(r["user_agent"]),
            "user_agent": r["user_agent"] or "",
            "ip": r["ip"] or "",
            "created_at": int(r["created_at"] or 0),
            "last_seen": int(r["last_seen"] or 0),
            "current": bool(current_token) and r["token"] == current_token,
        })
    return out


def revoke_any(conn, sid):
    """Admin: end ANY session by its public id, whoever owns it.

    Returns the user id it belonged to, or None when no such session exists.
    """
    if not sid:
        return None
    row = conn.execute("SELECT user_id FROM sessions WHERE sid=?", (sid,)).fetchone()
    if row is None:
        return None
    conn.execute("DELETE FROM sessions WHERE sid=?", (sid,))
    conn.commit()
    return row["user_id"]


def revoke_all_sessions(conn, keep_token=""):
    """Admin: end every session on the server, keeping one token when given so
    the button does not sign the admin out of the page they are on."""
    if keep_token:
        cur = conn.execute("DELETE FROM sessions WHERE token<>?", (keep_token,))
    else:
        cur = conn.execute("DELETE FROM sessions")
    conn.commit()
    return cur.rowcount


def account_for_verified_email(conn, email):
    """The user that already owns a VERIFIED email, or None.

    Only identities whose provider marked the address verified count here; an
    unverified address never claims a match.
    """
    email = (email or "").strip().lower()
    if not email:
        return None
    row = conn.execute(
        "SELECT user_id FROM identities WHERE email=? AND email_verified=1"
        " ORDER BY created_at LIMIT 1", (email,)).fetchone()
    return row["user_id"] if row is not None else None


def account_for_email(conn, email):
    """Any account that carries this address, verified or not, or None.

    Wider than account_for_verified_email on purpose: it is the "does this
    person already have an account?" question, not the "may I merge?" one. A
    hit here only ever sends a social login to the prove-it door; the address
    is untrusted, so it can never justify an automatic merge. Checks password
    accounts first (their address is collected at sign-up), then any social
    identity.
    """
    email = (email or "").strip().lower()
    if not email:
        return None
    row = conn.execute(
        "SELECT user_id FROM local_accounts WHERE email=? AND email<>''"
        " ORDER BY created_at LIMIT 1", (email,)).fetchone()
    if row is not None:
        return row["user_id"]
    row = conn.execute(
        "SELECT user_id FROM identities WHERE email=?"
        " ORDER BY created_at LIMIT 1", (email,)).fetchone()
    return row["user_id"] if row is not None else None


def upsert_user(conn, provider, ident):
    """Resolve a provider identity to one user id.

    An identity seen before keeps its user. A NEW identity is attached to the
    account it logs into, and never to an account just because the provider
    reports an email that account happens to own.

    A verified email proves control of the mailbox, not of the account. A
    password account's address is collected at sign-up and never verified, so
    it is not a merge key either: anyone could register victim@x, then the
    real owner's Google login would land in the seeded account. But it is the
    only join key a social login has to spot an account that already exists.
    So when a new identity's verified email matches ANY account carrying that
    address - a password account's unverified one, or a linked identity's - we
    refuse and raise AccountExistsError; the caller asks for that account's
    own login (auth.complete_claim) before we attach. The match never merges
    on its own. No verified email at all makes a fresh account, as before.
    """
    subject = ident["subject"]
    email = (ident.get("email") or "").strip().lower()
    verified = 1 if ident.get("email_verified") else 0
    row = conn.execute("SELECT user_id FROM identities WHERE provider=? AND subject=?",
                       (provider, subject)).fetchone()
    if row is not None:
        uid = row["user_id"]
    else:
        if verified and email:
            owner = account_for_email(conn, email)
            if owner is not None:
                raise AccountExistsError(owner, provider, ident)
        uid = "%s:%s" % (provider, subject)
        conn.execute("INSERT OR IGNORE INTO users(id,name,created_at) VALUES(?,?,?)",
                     (uid, ident.get("name", ""), _now()))
        _start_on_free_tier(conn, uid)
    conn.execute("INSERT OR REPLACE INTO identities"
                 "(provider,subject,user_id,email,email_verified,created_at)"
                 " VALUES(?,?,?,?,?,?)",
                 (provider, subject, uid, email, verified, _now()))
    conn.commit()
    return uid


# --- proving you own an existing account -----------------------------------
#
# When a social login's verified email already belongs to an account, we do not
# merge. We hold the pending identity in a login_claims row, keyed by an opaque
# token, and only attach it once the user proves ownership of the existing
# account - for a local account, by entering its password.

CLAIM_TTL = 900        # seconds; long enough to type a password, no longer


def create_claim(conn, provider, ident, user_id):
    """Stash a social identity that collided with an existing account."""
    token = secrets.token_urlsafe(24)
    now = int(time.time())
    conn.execute(
        "INSERT INTO login_claims"
        "(token,provider,subject,name,email,email_verified,user_id,created_at,expires_at)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (token, provider, ident["subject"], ident.get("name", ""),
         (ident.get("email") or "").strip().lower(),
         1 if ident.get("email_verified") else 0, user_id, now, now + CLAIM_TTL))
    conn.commit()
    return token


def take_claim(conn, token):
    """Read a pending claim, refusing one that has expired or was consumed."""
    row = conn.execute("SELECT * FROM login_claims WHERE token=?", (token,)).fetchone()
    if row is None:
        raise AuthError("this sign-in has expired; start again")
    if int(row["expires_at"] or 0) < int(time.time()):
        conn.execute("DELETE FROM login_claims WHERE token=?", (token,))
        conn.commit()
        raise AuthError("this sign-in has expired; start again")
    return row


USERNAME_RX = r"^[a-z0-9._-]+$"


def _check_username(conn, username):
    """Validate a sign-in name for an account that has never had one."""
    name = normalize_username(username)
    if len(name) < 3 or len(name) > 64:
        raise AuthError("pick a sign-in name: 3 to 64 characters")
    if not re.match(USERNAME_RX, name):
        raise AuthError("a sign-in name may use letters, digits, dot, dash or underscore")
    if conn.execute("SELECT 1 FROM local_accounts WHERE username=?", (name,)).fetchone():
        raise AuthError("that sign-in name is taken")
    return name


def set_password(conn, user_id, current_password, new_password, username=""):
    """Set or change the signed-in account's own password.

    A password account proves the old password first. A social-only account has
    none to prove, so it picks a sign-in name instead and gets a password; from
    then on either door works. Returns the sign-in name the account uses now, so
    a social-only user is told what to type next time.
    """
    if not user_id:
        raise AuthError("auth required")
    if len(new_password or "") < 8:
        raise AuthError("password must be at least 8 characters")
    salt = secrets.token_hex(16)
    if has_local_account(conn, user_id):
        if not local_password_proof(conn, user_id, current_password or ""):
            raise AuthError("current password is wrong")
        row = conn.execute("SELECT username FROM local_accounts WHERE user_id=?",
                           (user_id,)).fetchone()
        conn.execute("UPDATE local_accounts SET digest=?,salt=? WHERE user_id=?",
                     (_digest(new_password, salt), salt, user_id))
        name = row["username"] if row is not None else ""
    else:
        name = _check_username(conn, username)
        row = conn.execute("SELECT email FROM identities WHERE user_id=?"
                           " AND email<>'' LIMIT 1", (user_id,)).fetchone()
        email = (row["email"] if row is not None else "") or ""
        conn.execute("INSERT INTO local_accounts"
                     "(username,user_id,digest,salt,email,created_at)"
                     " VALUES(?,?,?,?,?,?)",
                     (name, user_id, _digest(new_password, salt), salt,
                      email, _now()))
    conn.commit()
    return name


# --- signing in from the Android app ------------------------------------------
# Google (and Apple, and Microsoft) refuse OAuth inside an embedded webview, so
# the app hands the flow to a browser tab and needs the finished session back.
# The tab cannot set the webview's cookie, so the callback mints a one-time
# handoff: a token, short-lived, single use, redeemable once at
# /auth/app/session. That URL is opened inside the webview and the cookie lands
# there. The custom-scheme hop is the weak link, so the token is worth seconds,
# not minutes, and it dies on first use.
APP_COOKIE = "fireside_app_return"
APP_HANDOFF_TTL = 120

def mint_app_handoff(conn, user_id):
    """A one-time token that turns into a session inside the app's webview."""
    if not user_id:
        raise AuthError("auth required")
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    conn.execute("DELETE FROM app_handoffs WHERE user_id=?", (user_id,))
    conn.execute("INSERT INTO app_handoffs"
                 "(token_hash,user_id,created_at,expires_at,used_at)"
                 " VALUES(?,?,?,?,0)",
                 (_hash_reset_token(token), user_id, now,
                  now + APP_HANDOFF_TTL))
    conn.commit()
    return token


def take_app_handoff(conn, token):
    """Consume a handoff and return the user it was minted for.

    Unknown, expired and already-used all read the same way: the app shows the
    sign-in door again, which is the correct next step in every one of them.
    """
    h = _hash_reset_token(token or "")
    row = conn.execute("SELECT user_id,expires_at,used_at FROM app_handoffs"
                       " WHERE token_hash=?", (h,)).fetchone()
    if row is None:
        raise AuthError("that sign-in has expired; start again")
    if int(row["used_at"] or 0) or int(row["expires_at"] or 0) < int(time.time()):
        raise AuthError("that sign-in has expired; start again")
    conn.execute("UPDATE app_handoffs SET used_at=? WHERE token_hash=?",
                 (int(time.time()), h))
    conn.commit()
    return row["user_id"]


def local_password_proof(conn, user_id, password):
    """True when `password` is the local account password for `user_id`.

    Constant-time compare; a user with no local account always fails here,
    because a social-only account cannot be proven by a password it never had.
    """
    row = conn.execute("SELECT digest,salt FROM local_accounts WHERE user_id=?",
                       (user_id,)).fetchone()
    if row is None:
        return False
    return secrets.compare_digest(_digest(password or "", row["salt"]), row["digest"])


def has_local_account(conn, user_id):
    return conn.execute("SELECT 1 FROM local_accounts WHERE user_id=?",
                        (user_id,)).fetchone() is not None


def claim_methods(conn, user_id):
    """How the owner of an account can prove it: a password it has, and the
    providers already attached to it. Logging in the way the account already
    does IS the proof - a password is only one door, not the door."""
    return {
        "has_password": has_local_account(conn, user_id),
        "providers": linked_providers(conn, user_id),
    }


def complete_claim(conn, token, password):
    """Attach a pending social identity once the account's password is proven.

    Returns the user id on success. Raises AuthError when the account has no
    password to prove with - the other door is complete_claim_via_identity:
    sign in through a provider the account already uses.
    """
    claim = take_claim(conn, token)
    if not has_local_account(conn, claim["user_id"]):
        raise AuthError("that account has no password; sign in the way it "
                        "already does instead")
    if not local_password_proof(conn, claim["user_id"], password):
        raise AuthError("that password does not match the existing account")
    return _finish_claim(conn, token, claim)


def complete_claim_via_identity(conn, token, provider, ident):
    """Attach a pending social identity after proving ownership by signing in
    as the existing account through one of ITS OWN providers.

    The proof is that the identity just authenticated already belongs to the
    claim's account. An identity owned by someone else (or by nobody) proves
    nothing and is refused - otherwise a second social login could seize the
    account.
    """
    claim = take_claim(conn, token)
    subject = ident.get("subject") or ""
    owner = identity_user(conn, provider, subject)
    if owner is None or owner != claim["user_id"]:
        raise AuthError("that login is not one this account already uses")
    return _finish_claim(conn, token, claim)


def _finish_claim(conn, token, claim):
    """Link the pending identity to the claim's account and consume the claim."""
    ident = {"subject": claim["subject"], "name": claim["name"],
             "email": claim["email"],
             "email_verified": bool(claim["email_verified"])}
    link_identity(conn, claim["provider"], ident, claim["user_id"])
    conn.execute("DELETE FROM login_claims WHERE token=?", (token,))
    conn.commit()
    return claim["user_id"]


def identity_user(conn, provider, subject):
    """The user a provider identity already resolves to, or None."""
    row = conn.execute("SELECT user_id FROM identities WHERE provider=? AND subject=?",
                       (provider, subject)).fetchone()
    return row["user_id"] if row is not None else None


def link_identity(conn, provider, ident, user_id):
    """Attach a provider identity to an explicit, already-authenticated user.

    This is the user-initiated door (settings > link an account), not the
    login door. It refuses when the identity already belongs to a DIFFERENT
    account - otherwise a link flow could be used to seize someone's login -
    and re-linking to the same user is idempotent.
    """
    subject = ident["subject"]
    existing = identity_user(conn, provider, subject)
    if existing is not None and existing != user_id:
        raise AuthError("this %s account is already linked to another user" % provider)
    email = (ident.get("email") or "").strip().lower()
    verified = 1 if ident.get("email_verified") else 0
    conn.execute("INSERT OR REPLACE INTO identities"
                 "(provider,subject,user_id,email,email_verified,created_at)"
                 " VALUES(?,?,?,?,?,?)",
                 (provider, subject, user_id, email, verified, _now()))
    conn.commit()
    return user_id


def linked_providers(conn, user_id):
    """Which social providers a user has attached to their account."""
    if not user_id:
        return []
    rows = conn.execute("SELECT DISTINCT provider FROM identities WHERE user_id=?",
                        (user_id,)).fetchall()
    return sorted(r["provider"] for r in rows)


class Auth:
    def __init__(self, conn, fetch=None, providers=None, local=None):
        self.conn = conn
        self.fetch = fetch or _default_fetch
        self.table = providers or PROVIDERS
        self.allow_local = _allow_local() if local is None else bool(local)

    def _creds(self, pid):
        cid, sec = self.table[pid]["id_env"]
        return os.environ.get(cid, ""), os.environ.get(sec, "")

    def providers(self):
        out = []
        if self.allow_local:
            out.append({"id": "local", "label": "Local account", "kind": "local"})
        for pid, cfg in self.table.items():
            cid, sec = self._creds(pid)
            if cid and sec:
                out.append({"id": pid, "label": cfg["label"],
                            "start": "/auth/%s/start" % pid})
        return out

    def authorize_url(self, pid, redirect_uri, state, verifier=""):
        cfg = self.table[pid]
        cid, _ = self._creds(pid)
        if not cid:
            raise AuthError("provider %s is not configured" % pid)
        q = {"client_id": cid, "redirect_uri": redirect_uri, "scope": cfg["scope"],
             "state": state, "response_type": "code"}
        if cfg.get("pkce"):
            if not verifier:
                raise AuthError("provider %s needs a pkce verifier" % pid)
            digest = hashlib.sha256(verifier.encode("ascii")).digest()
            q["code_challenge"] = _b64url(digest)
            q["code_challenge_method"] = "S256"
        if pid == "google":
            q["access_type"] = "online"
            q["prompt"] = "select_account"
        return cfg["authorize"] + "?" + urllib.parse.urlencode(q)

    def _exchange(self, pid, code, redirect_uri, verifier=""):
        cfg = self.table[pid]
        cid, sec = self._creds(pid)
        body = {"client_id": cid, "client_secret": sec, "code": code,
                "redirect_uri": redirect_uri, "grant_type": "authorization_code"}
        headers = {"Accept": "application/json",
                   "Content-Type": "application/x-www-form-urlencoded"}
        if cfg.get("pkce"):
            # X wants the verifier back, and the client proven per RFC 6749
            # (HTTP Basic) instead of a secret in the body.
            body["code_verifier"] = verifier or ""
            headers["Authorization"] = "Basic " + _b64url(
                ("%s:%s" % (cid, sec)).encode())
        data = urllib.parse.urlencode(body).encode()
        req = urllib.request.Request(cfg["token"], data=data, headers=headers)
        body = json.loads(self.fetch(req).decode("utf-8"))
        tok = body.get("access_token")
        if not tok:
            raise AuthError("no access_token from %s: %s" % (pid, body.get("error", "unknown")))
        return tok

    def _get_json(self, url, token):
        req = urllib.request.Request(url, headers={
            "Authorization": "Bearer " + token, "Accept": "application/json",
            "User-Agent": "fireside"})
        return json.loads(self.fetch(req).decode("utf-8"))

    def identity(self, pid, access_token):
        cfg = self.table[pid]
        ident = _identity(pid, self._get_json(cfg["identity"], access_token))
        if pid == "github" and cfg.get("emails"):
            # GitHub's /user carries no verified flag; /user/emails does. Fetch it
            # so a github address is only ever linked on a verified one.
            try:
                emails = self._get_json(cfg["emails"], access_token)
                primary = next((e for e in emails if e.get("primary")),
                               emails[0] if emails else None)
                if primary:
                    ident["email"] = primary.get("email") or ident["email"]
                    ident["email_verified"] = bool(primary.get("verified"))
            except Exception:
                pass
        if pid == "simplelogin":
            # The Proton door. SimpleLogin hands back the signed-in account's own
            # address with no explicit flag; that address is confirmed when the
            # account is made, so it is the join key the social door can use.
            # (email_verified stays the guard: only a trusted address may route
            # a login to the prove-it door.)
            ident["email_verified"] = bool(ident.get("email"))
        return ident

    def identity_from_code(self, pid, code, redirect_uri, verifier=""):
        """Exchange a callback code and return the normalized identity, without
        touching users or sessions. Used by the claim-proof path, where the
        identity itself is the evidence."""
        if pid not in self.table:
            raise AuthError("unknown provider %s" % pid)
        access = self._exchange(pid, code, redirect_uri)
        ident = self.identity(pid, access)
        if not ident["subject"]:
            raise AuthError("provider %s returned no subject" % pid)
        return ident

    def login(self, pid, code, redirect_uri, ua="", ip="", verifier=""):
        if pid not in self.table:
            raise AuthError("unknown provider %s" % pid)
        access = self._exchange(pid, code, redirect_uri, verifier)
        ident = self.identity(pid, access)
        if not ident["subject"]:
            raise AuthError("provider %s returned no subject" % pid)
        uid = upsert_user(self.conn, pid, ident)
        return uid, create_session(self.conn, uid, pid, ua=ua, ip=ip)

    def link(self, pid, code, redirect_uri, user_id, verifier=""):
        """Finish a link flow: attach this provider to an existing user."""
        if pid not in self.table:
            raise AuthError("unknown provider %s" % pid)
        access = self._exchange(pid, code, redirect_uri, verifier)
        ident = self.identity(pid, access)
        if not ident["subject"]:
            raise AuthError("provider %s returned no subject" % pid)
        return link_identity(self.conn, pid, ident, user_id)



# ---- password reset (a link you can only use once, and briefly) -------------
# A password account's address is collected at sign-up and never verified, so
# the reset door cannot prove it *owns* the account - all it can do is send a
# link to whoever can read that inbox and let that person set a new password.
# That is the whole security model, and it is why the flow says nothing about
# whether an address matched: the answer would leak who has an account here.

RESET_TTL = 3600            # one hour; long enough to find the mail, no longer


def _hash_reset_token(token):
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def request_password_reset(conn, email):
    """Mint a reset token for the password account that owns `email`.

    Returns (raw_token, username) so the caller can mail the link, or None when
    no password account carries the address (or it is empty). The raw token is
    never stored - only its hash - so a read of the table cannot be replayed.

    Any live reset for the account is retired first: one link at a time, so the
    most recent request is the one that works.
    """
    email = (email or "").strip().lower()
    if not email:
        return None
    row = conn.execute(
        "SELECT user_id,username FROM local_accounts WHERE email=? LIMIT 1",
        (email,)).fetchone()
    if row is None:
        return None
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    conn.execute("DELETE FROM password_resets WHERE user_id=?", (row["user_id"],))
    conn.execute(
        "INSERT INTO password_resets(token_hash,user_id,created_at,expires_at,used_at)"
        " VALUES(?,?,?,?,0)",
        (_hash_reset_token(token), row["user_id"], now, now + RESET_TTL))
    conn.commit()
    return token, row["username"]


def reset_password(conn, token, new_password):
    """Consume a reset token and set a new password.

    On success every existing session of the account is revoked: a stolen
    session dies with the password it was won under. The token is marked used,
    not deleted, so a replay reads as 'no longer valid' rather than 'never
    existed'.
    """
    if len(new_password or "") < 8:
        raise AuthError("password must be at least 8 characters")
    h = _hash_reset_token(token)
    row = conn.execute(
        "SELECT user_id,expires_at,used_at FROM password_resets WHERE token_hash=?",
        (h,)).fetchone()
    if row is None or row["used_at"]:
        raise AuthError("that reset link is no longer valid")
    if int(row["expires_at"] or 0) < int(time.time()):
        raise AuthError("that reset link has expired")
    uid = row["user_id"]
    salt = secrets.token_hex(16)
    conn.execute("UPDATE local_accounts SET digest=?,salt=? WHERE user_id=?",
                 (_digest(new_password, salt), salt, uid))
    conn.execute("UPDATE password_resets SET used_at=? WHERE token_hash=?",
                 (int(time.time()), h))
    conn.commit()
    revoke_sessions(conn, uid)
    return uid
