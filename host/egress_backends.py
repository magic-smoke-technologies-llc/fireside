"""Fetch backends for host-owned egress. See docs/SOURCE-CONTRACT.md.

A source names a backend, never a client library. Each backend hides its own
dependency here, so the core carries no vendor vocabulary and swapping a client
touches exactly one file.

  plain    stdlib urllib. The default; no third-party dependency.
  browser  presents a browser TLS/HTTP fingerprint, for sites whose bot check
           grades the ClientHello rather than the request.

Every backend has the same shape:

    fn(url, headers, limit, timeout, opts) -> text    (raises on failure)

`opts` is the source's declared transport dict. A backend reads the keys it
knows and ignores the rest, so the generic policy (headers, retries, backoff,
timeout) lives in the host while a backend's own knobs live here.
"""

import urllib.parse
import urllib.request

from host import netguard


class HttpStatusError(RuntimeError):
    """A non-2xx answer, with the status and the head of the body attached.

    The retry loop has no business guessing what a site meant, and the host
    must not learn one site's vocabulary. So the backend hands the raw facts
    over (status, url, a short body) and a source that knows its site can name
    the cause in the user's words (AO3's "Shields are up!" page is a 403 that
    is not a refusal of that link at all).
    """

    SNIPPET = 3000

    def __init__(self, status, url, body=""):
        super().__init__("HTTP %d from %s" % (status, url))
        self.status = status
        self.url = url
        self.body = (body or "")[:self.SNIPPET]


def fetch_plain(url, headers, limit, timeout, opts):
    req = urllib.request.Request(url, headers=headers)
    with netguard.opener().open(req, timeout=timeout) as r:
        raw = r.read(limit)
        cs = r.headers.get_content_charset()
    return raw.decode(cs or "utf-8", "replace")


def fetch_browser(url, headers, limit, timeout, opts):
    """Present a browser TLS/HTTP fingerprint. The client (curl_cffi) is imported
    lazily so the dependency is only needed when a source actually declares this
    backend. `opts["profile"]` names the browser to imitate; the backend owns the
    fallback. Raises on non-2xx so the retry loop can back off (challenge and
    rate-limit responses are often transient)."""
    try:
        from curl_cffi import requests as creq
    except Exception as e:  # pragma: no cover - dependency-missing path
        raise RuntimeError(
            "this source needs the 'browser' fetch backend, which uses curl_cffi "
            "(pip install curl_cffi): %s" % e)
    # One fingerprint can go stale: the same site that answers a Safari
    # profile can hang a Chrome one for a minute and then the other way round.
    # A source may name a list ("profiles"); each is tried once, first answer
    # wins, and the last failure is what the caller sees.
    main = opts.get("profile") or "safari17_0"
    order = [main] + [p for p in (opts.get("profiles") or []) if p != main]
    last = None
    for prof in order:
        sess = creq.Session(impersonate=prof)
        try:
            # Follow redirects here, not in the client, so each hop is checked.
            at = url
            for _hop in range(netguard.MAX_REDIRECTS + 1):
                r = sess.get(at, headers=headers, timeout=timeout, allow_redirects=False)
                loc = r.headers.get("Location") if 300 <= r.status_code < 400 else None
                if not loc:
                    break
                at = urllib.parse.urljoin(at, loc)
                netguard.check_public(at)
            else:
                raise RuntimeError("too many redirects")
        except netguard.BlockedAddress:
            raise
        except Exception as e:
            last = e
            continue
        if r.status_code >= 400:
            last = HttpStatusError(r.status_code, at, r.text)
            continue
        return r.text[:limit]
    raise last


BACKENDS = {
    "plain": fetch_plain,
    "browser": fetch_browser,
}
DEFAULT_BACKEND = "plain"
