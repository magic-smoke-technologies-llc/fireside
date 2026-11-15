"""Keep outbound fetches on the public internet.

A story link or a cover URL is typed by whoever is signed in, and the box that
fetches it usually sits next to things that trust it: the loopback port this
very server listens on, a database, the cloud metadata address. So a fetch of a
user-supplied URL must not reach a private address, whether the URL names one
directly or a public host redirects to one.

What this does not do: it resolves the name here and the HTTP client resolves it
again to connect, so a hostile DNS server that answers differently the second
time (rebinding) still gets through. Closing that means connecting to the
address checked here, which is a transport change and is left open.

An operator who really does import from the local network sets
FIRESIDE_ALLOW_PRIVATE_FETCH=1.
"""

import ipaddress
import os
import socket
import urllib.parse
import urllib.request

MAX_REDIRECTS = 5


class BlockedAddress(ValueError):
    """The URL points at (or redirects to) an address this box will not fetch."""


def _allowed_private():
    return (os.environ.get("FIRESIDE_ALLOW_PRIVATE_FETCH") or "").strip().lower() \
        in ("1", "true", "yes", "on")


def _public(ip):
    ip = ipaddress.ip_address(ip)
    # ::ffff:127.0.0.1 is loopback to the kernel but reads as a v6 address.
    mapped = getattr(ip, "ipv4_mapped", None)
    return (mapped or ip).is_global


def check_public(url):
    """Raise BlockedAddress unless `url` is http(s) to a public host.

    A host that does not resolve passes: the fetch fails on its own with the
    resolver's error, which is a better message than ours.
    """
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise BlockedAddress("only http and https links are fetched")
    if _allowed_private():
        return
    try:
        infos = socket.getaddrinfo(
            parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
    except OSError:
        return
    for info in infos:
        try:
            public = _public(info[4][0].split("%")[0])
        except ValueError:
            raise BlockedAddress("could not read the address for that link")
        if not public:
            raise BlockedAddress("that link points at a private network address")


def is_public_url(url):
    try:
        check_public(url)
    except BlockedAddress:
        return False
    except ValueError:
        return False
    return True


class _GuardedRedirects(urllib.request.HTTPRedirectHandler):
    max_redirections = MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_public(urllib.parse.urljoin(req.full_url, newurl))
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def opener():
    """A urllib opener whose every redirect hop is checked."""
    return urllib.request.build_opener(_GuardedRedirects())
