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
    sess = creq.Session(impersonate=opts.get("profile") or "safari17_0")
    # Follow redirects here, not in the client, so each hop is checked.
    for _hop in range(netguard.MAX_REDIRECTS + 1):
        r = sess.get(url, headers=headers, timeout=timeout, allow_redirects=False)
        loc = r.headers.get("Location") if 300 <= r.status_code < 400 else None
        if not loc:
            break
        url = urllib.parse.urljoin(url, loc)
        netguard.check_public(url)
    else:
        raise RuntimeError("too many redirects")
    if r.status_code >= 400:
        raise RuntimeError("HTTP %d from %s" % (r.status_code, url))
    return r.text[:limit]


BACKENDS = {
    "plain": fetch_plain,
    "browser": fetch_browser,
}
DEFAULT_BACKEND = "plain"
