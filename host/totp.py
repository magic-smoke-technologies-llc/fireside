"""TOTP (RFC 6238) for the second factor, stdlib only.

Why hand-rolled instead of adding pyotp: the app is stdlib-first on purpose,
and this is thirty lines of HMAC with published test vectors. A dependency
here would be a dependency at the one place where being wrong is worst.

Nothing in this module touches the database. It answers two questions only:
what code does this secret produce at this moment, and does this code match.
Replay protection and enrolment live in host/auth.py, because they need state.
"""

import base64
import hashlib
import hmac
import secrets
import struct
import time

DIGITS = 6
STEP = 30
ALGO = "sha1"          # what every authenticator app assumes
ISSUER = "Fireside"
SECRET_BYTES = 20      # RFC 4226 recommends 160 bits
RECOVERY_CODES = 10


def new_secret(nbytes=SECRET_BYTES):
    """A fresh base32 secret, no padding, the shape authenticator apps expect."""
    return base64.b32encode(secrets.token_bytes(nbytes)).decode("ascii").rstrip("=")


def _key(secret):
    s = "".join(str(secret).split()).upper().rstrip("=")
    pad = (-len(s)) % 8
    try:
        return base64.b32decode(s + "=" * pad)
    except Exception:
        return b""


def hotp(secret, counter, digits=DIGITS, algo=ALGO):
    """RFC 4226. Returns the digits as a zero-padded string."""
    key = _key(secret)
    if not key:
        return ""
    msg = struct.pack(">Q", int(counter))
    mac = hmac.new(key, msg, getattr(hashlib, algo)).digest()
    off = mac[-1] & 0x0F
    code = struct.unpack(">I", mac[off:off + 4])[0] & 0x7FFFFFFF
    return str(code % (10 ** digits)).zfill(digits)


def counter_at(at=None, step=STEP):
    return int((time.time() if at is None else at) // step)


def totp(secret, at=None, step=STEP, digits=DIGITS, algo=ALGO):
    return hotp(secret, counter_at(at, step), digits, algo)


def _clean(code):
    return "".join(ch for ch in str(code or "") if ch.isdigit())


def verify(secret, code, at=None, step=STEP, digits=DIGITS, algo=ALGO, window=1):
    """Check a code. Returns the counter it matched, or None.

    window=1 accepts the previous, current and next step, so a phone whose
    clock drifts a few seconds still gets in. The returned counter is what the
    caller stores to refuse a replay of the same code.
    """
    given = _clean(code)
    if len(given) != digits or not _key(secret):
        return None
    now = counter_at(at, step)
    for c in range(now - window, now + window + 1):
        if hmac.compare_digest(hotp(secret, c, digits, algo), given):
            return c
    return None


def provisioning_uri(secret, account, issuer=ISSUER):
    """otpauth:// URI, what goes in the QR and behind the manual-entry link."""
    from urllib.parse import quote

    label = "%s:%s" % (quote(issuer, safe=""), quote(str(account or "account"), safe=""))
    return ("otpauth://totp/%s?secret=%s&issuer=%s&algorithm=%s&digits=%d&period=%d"
            % (label, secret, quote(issuer, safe=""), ALGO.upper(), DIGITS, STEP))


def new_recovery_codes(n=RECOVERY_CODES, groups=2, size=5):
    """Human-copyable one-time codes: 10 of them, alphabet without look-alikes."""
    alphabet = "23456789abcdefghjkmnpqrstuvwxyz"
    out = []
    while len(out) < n:
        groups_txt = ["".join(secrets.choice(alphabet) for _ in range(size))
                      for _ in range(groups)]
        out.append("-".join(groups_txt))
    return out
