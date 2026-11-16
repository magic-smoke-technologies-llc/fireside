"""Host-owned HTTP egress for sources. See docs/SOURCE-CONTRACT.md.

The plugin never opens a socket. It calls the `fetch` the host hands it; the host
attaches the credential (only to hosts the source declared), and logs every
request. This module is that fetch, plus the credential-scoping check.

Transport (added v1.10): some sites gate on the TLS ClientHello, not on routing.
A source that hits one of those declares `transport()`, which names a fetch
`backend` (see _backends.py) plus generic policy -- headers, retries, backoff,
timeout. The host performs the fetch through this one logged path, still with
the cookie attached only to declared hosts. The source owns the quirk; the host
owns the socket, the credential, and the choice of client.
"""

import time
import urllib.parse
import urllib.request

from host import netguard
from host.egress_backends import BACKENDS, DEFAULT_BACKEND

UA = "Mozilla/5.0 (compatible; reader-local/1.0; personal read-aloud)"
DEFAULT_LIMIT = 8 * 1024 * 1024
DEFAULT_TIMEOUT = 30


def host_allowed(url: str, credential_hosts) -> bool:
    """True if `url`'s host is inside the source's declared credential scope.
    Matching is on host or a dot-boundary suffix (archiveofourown.org matches
    www.archiveofourown.org, not notarchiveofourown.org)."""
    if not credential_hosts:
        return False
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    for h in credential_hosts:
        h = h.lower().lstrip(".")
        if host == h or host.endswith("." + h):
            return True
    return False


def fetch(url: str, cred=None, credential_hosts=(), limit: int = DEFAULT_LIMIT,
          transport=None):
    """Fetch one url and return text. Attaches the credential as a Cookie header
    ONLY when the host is in scope. Never logs the credential. `transport` is the
    source's declared fetch policy (see docs/SOURCE-CONTRACT.md): it names a
    `backend` and carries generic policy. None = plain urllib, same as before."""
    u = urllib.parse.urlparse(url)
    if u.scheme not in ("http", "https"):
        raise ValueError("only http/https links")
    netguard.check_public(url)      # before the retry loop: a refusal is not transient
    t = transport or {}
    backend = t.get("backend") or DEFAULT_BACKEND
    fn = BACKENDS.get(backend)
    if fn is None:
        raise ValueError("unknown fetch backend %r (known: %s)"
                         % (backend, ", ".join(sorted(BACKENDS))))
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
    }
    headers.update(t.get("headers") or {})
    if cred and host_allowed(url, credential_hosts):
        cookie = cred.get("cookie") or cred.get("session") or ""
        if cookie:
            headers["Cookie"] = cookie
    timeout = t.get("timeout", DEFAULT_TIMEOUT)
    retries = int(t.get("retries", 0))
    backoff = float(t.get("backoff", 1.5))
    last = None
    for attempt in range(retries + 1):
        try:
            return fn(url, headers, limit, timeout, t)
        except netguard.BlockedAddress:
            raise                    # a redirect hop into a private network
        except Exception as e:
            last = e
            if attempt < retries:
                time.sleep(backoff * (attempt + 1))
    raise last


def bind_transport(base_fetch, transport):
    """Wrap a fetch so it carries a source's declared transport, without the
    source call-sites knowing. Returns base_fetch unchanged when transport is
    falsy, so plain sources fetch exactly as before."""
    if not transport:
        return base_fetch

    def bound(url, cred=None, credential_hosts=()):
        return base_fetch(url, cred=cred, credential_hosts=credential_hosts,
                          transport=transport)

    return bound
