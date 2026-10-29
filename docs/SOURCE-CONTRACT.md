# Source contract (v1)

The app already has two sources hardcoded: an EPUB upload and a generic web-page
fetcher, handled inline in `_ingest` / `_ingest_url`. That is the ingest-side
twin of the speech-provider seam: nobody named it, and unnamed seams drift.

The idea (Chris, 2026-10-16): a plugin per source. An EPUB reader, a generic
http reader, and later an AO3 reader that detects an AO3 URL and knows how to
scrape the needed text. Each source can surface its own screen under settings
(e.g. paste an AO3 session cookie so locked fics come through).

The loader is later. The contract is the part that stops the two existing
sources from growing apart, and that a third source can be written against.

## The interface

One source = one module exposing:

1. `id` - stable slug (`epub`, `web`, `ao3`, ...). It is part of the book
   identity and the meter tag. Never rename it once books exist under it.
2. `matches(req) -> bool`. `req = {kind: "upload"|"url", filename, data, url}`.
3. `read(req, fetch, cred) -> book`,
   `book = {title, chapters: [{title, text}], meta: {...}}`. The shape is exactly
   what `parse_epub` / `parse_web` already return, so nothing downstream changes.
4. `settings() -> [Field]`, `Field = {key, label, type: "text"|"secret"|"select",
   help, placeholder}`. Declarative. The host renders the widget. **A source
   ships no HTML**, same reason a provider never holds the login.
5. `credential_hosts() -> [host suffix]`. Host suffixes the credential may be
   sent to; `[]` if the source takes no credential.

## Who performs the request (Q1: my call)

Not plugin, not a naked host: split it.

- The **host owns egress and the credential.** The plugin never opens a socket,
  never holds the login, and never learns which door the credential came through
  (paste-cookie now, a real sign-in later). Every fetch is logged and metered in
  one place.
- The **plugin owns matching and parsing.** It calls the `fetch(url)` the host
  handed it, then maps the response to a book.

A plugin with its own sockets would be a second place credentials can leak and a
second place requests are not audited. Handing it a `fetch` keeps the AO3-specific
scraping logic where it belongs while leaving one auditable chokepoint.

## Credential scoping

The credential only leaves to the hosts a source declared in
`credential_hosts()`, attached as a `Cookie` header by the host. For AO3 that is
`archiveofourown.org` (matched on host or a dot boundary, so `www.` counts and
`notarchiveofourown.org` does not). The credential is never logged.

## Credentials: paste-cookie now (Q3)

One slot per (source, user). `paste-cookie` now; a real sign-in flow later - two
doors, one slot, exactly like a provider key. The cookie is a `secret` Field.

- Local app: kept in the browser (localStorage) and sent per request.
- Hosted: stored server-side per user, encrypted, never returned to the client.

## Precedence

Sources are tried in registration order; first `matches()` wins. The generic
`web` source is the fallback and **must be registered last**. `epub` matches any
upload. Ordering guarantees only one source consumes a request, so book ids
cannot collide across sources.

## Book identity

The host computes `book_id = sha1(canonical request)`:

- upload -> `sha1(bytes)[:12]`
- url    -> `sha1(url)[:12]`

Same url re-ingested is idempotent (same book).

## Drift, named and closed (v1)

The two sources now live in `sources/` on this contract: `epub.py`, `web.py`,
plus the registry in `sources/__init__.py`.

- `server.py`'s `_ingest` and `_ingest_url` call `sources.ingest(req, cred)`
  instead of calling `parse_epub` / `parse_web` directly; the parsers moved out
  of `server.py` into the source modules.
- The host's HTTP egress moved to `sources/_fetch.py`, which enforces the
  credential scope and is the one place a source's requests pass through.
- `book_id` now comes from `sources.request_key`, one definition for both kinds.
- `tests/test_sources.py` checks the registry, the parsers on plain strings, the
  credential-scope rule, and the real HTTP ingest flow for an EPUB upload, with
  no external network.

Still later: the loader (discovery of third-party source plugins, isolation,
install). That is expansion, not revenue: do not let it eat the hosted-billing
path.

## AO3 shipped (v1.9)

`ao3.py` is a real source on this contract: it matches an
`archiveofourown.org/works/<id>` link, asks AO3 for `view_full_work=true` so one
book has all N chapters (each chapter is its own audio file downstream), and
scrapes the body out of AO3's own markup. Locked works use the reader's session
cookie: pasted in Settings, kept in the browser, sent only to
`archiveofourown.org`. It is heavier than epub/web because it acts with the
user's login, which is exactly why the credential lives host-side and is scoped.

The parse targets HTML, not an API, so an AO3 re-skin can break it; a login page
or the adult gate is detected and reported instead of silently returning junk.

## Settings surface (v1.9)

`describe()` returns the whole registry as plain data: each source's `id`,
`label`, `settings()` fields, and `credential_hosts()`. The host renders one
Settings page from that (see the Text sources panel in `index.html`); a source
still ships no HTML. On `ingest-url` the host resolves the credential per source
after matching (`body.creds[source.id]`), so a source never sees credentials
meant for another.

## Transport (v1.10, revised v1.12)

"> How is the text fetched? Does the plugin define it? That was the hope: the plugin could own the quirks
> of connecting to whatever platform." - Chris, 2026-10-16

It does now, without the core learning any one platform's or vendor's name. A
source may declare

    transport() -> {"backend": "browser", "profile": "safari17_0",
                    "retries": 4, "backoff": 1.5, "timeout": 60}

and the host performs the fetch that way. `backend` names one of the host's
fetch backends (`plain` = stdlib urllib, the default; `browser` = a browser
TLS/HTTP fingerprint). Generic fields: `headers`, `retries`, `backoff`,
`timeout`. A backend reads its own knobs (`profile` is the browser to imitate);
the host ignores what it does not know. Absent `transport()` = `plain`.

The backend's client library lives entirely inside `sources/_backends.py`. The
core egress (`sources/_fetch.py`) speaks only "backend", never a vendor's word
and never a vendor's import; adding or swapping a client touches one file. This
is the rule Chris stated on 2026-10-16: **the system is shaped to host
integrations; integrations flex around the external system. Never adapt the
system to a quirk of an integration.** `test_sources.py` asserts it (the core
carries no vendor token).

Why `browser` exists at all: `archiveofourown.org` is behind Cloudflare, which
grades the TLS ClientHello. Measured from the sandbox on 2026-10-16, same URL:

    plain urllib           -> HTTP 525 (SSL handshake failed)
    chrome fingerprint     -> HTTP 403
    safari fingerprint     -> HTTP 200, full work

So the 525 is not a routing problem; it is the bot check refusing the client. A
raw TLS handshake to AO3 from the bench box completes (TLSv1.3) and then
Cloudflare answers 525. The gate is also rate-sensitive: a burst flips a good
request back to 525, which is what `retries`/`backoff` are for.

This keeps the earlier split intact. The host still owns the socket and the
credential (one audited chokepoint, cookie only to declared hosts); the source
owns the quirk of how the site wants to be fetched.

## Still later

- The loader: discovery of third-party source plugins, isolation, install. That
  is expansion, not revenue: do not let it eat the hosted-billing path.
- Signed history and a hosted multi-user credential store (server-side,
  encrypted) for the credential that is browser-side today.


## v2: the readers live in the plugins (2026-10-23)

Chris's call, after two special cases had grown in-tree: pull the sources out
into real plugin packages, not adapters over `sources/<site>.py`.

    plugins/reader/epub/plugin.py         uploads
    plugins/reader/ao3/plugin.py          AO3 works (browser transport + cookie)
    plugins/reader/keiramarcos/plugin.py  keiramarcos.com: bundle pages hold chapters
    plugins/reader/quantumbang/plugin.py  a story split across numbered pages
    plugins/reader/web/plugin.py          any other http(s) link (fallback)

Each folder carries its own matching, parsing, settings and transport, and
imports no other plugin. Shared machinery moved to the host, where it belongs:

- `host/egress.py` - the one socket, credential scope, transport policy
  (was `sources/_fetch.py`)
- `host/egress_backends.py` - plain / browser (was `sources/_backends.py`)
- `host/textutil.py` - element -> text, and page -> the container worth reading
  (was `sources/_text.py`; the page reader is generic HTML, not site knowledge,
  so every reader that reads article pages shares it)
- `host/contracts.py` - `Field` gained `placeholder` and `.dict()`, so a plugin
  declares a settings row without shipping one

`sources/__init__.py` is now a v1 shim only: the same registry, built by
discovering the plugins, for the old `server.py` and the v1 tests. Adding a
site = adding a folder under `plugins/reader/` + a `plugin.json`. Nothing in the
host, nothing in `sources/`.

## quantumbang: parts, not pages (2026-10-23)

A Quantum Bang story is published as N pages whose slug carries the part:

    hidden-resources-for-young-men-of-quality-1-5-meyarimcfarland
                                              ^^^  part 1 of 5

So a page's family key is its slug minus any `-N/M-` segment, and every page of
one story shares it. The reader takes any page of the story (landing page or a
part), collects the family from the page's own links, fetches each part once,
and returns one book with chapters `Part 1 of N` ... in order.

Two deliberate refusals:

- **It does not follow `rel="next"`.** On this site that chain walks out of the
  story into whatever else the tag links to (the reason part-chaining was
  flagged as unsafe before). The slug family is the guard, so the crawl stops
  where the story stops.
- **It does not read the repeated metadata block.** Each part repeats
  `Title: / Author: / Fandom: / ... / Summary:`. That block is parsed once into
  the book's meta and dropped from every chapter, so warnings are not read
  aloud between parts. A "header" needs at least three `Key: value` paragraphs,
  so a story line containing a colon is never eaten.

Comment threads and related-post widgets are dropped by the shared page reader,
so a chapter never ends with "4 Comments: Bravo! Leave a Reply".
