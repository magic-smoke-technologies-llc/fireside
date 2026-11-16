"""Module-level helpers the App and its route mixins share.

Split out of host/app.py (which re-exports every name here) so the mixins can
import them without importing app.py back.
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
import zipfile
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


def _int_env(name, default):
    try:
        return max(0, int(os.environ.get(name, "")))
    except ValueError:
        return default


def _truthy(v):
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")
