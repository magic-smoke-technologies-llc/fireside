# fireside

A book, or a story link, turned into audio you can play, pause, resume, and keep.

fireside renders each chapter to a finished audio file **before** it plays, so
playback never stalls partway the way a live browser read-aloud can. Close the
tab, come back later, it picks up where you left off. Point it at an EPUB or a
public story URL, pick a voice, and go.

One server runs in three shapes, and the shape is a setting, not a different
install:

| shape        | who it is for                        | what changes                                    |
|--------------|--------------------------------------|-------------------------------------------------|
| one reader   | you, on a laptop or a small always-on box | nothing; this is the default                |
| a household  | a few people who share a box         | accounts, each with their own shelf and place    |
| hosted       | the open internet                    | TLS in front, sign-ups and a payment provider    |

Multi-user is not a fork or an add-on. The first account on a fresh server
becomes its admin, and everything after that is a toggle on the admin screen.

## Run it

### Docker (recommended)

    docker compose up -d
    # open http://<this-machine>:8788

State (the SQLite database and the audio blobs) lives in `./data`, mapped to
`/data` in the container, so an update never loses your library.

### No Docker: Python and one install step

The default backend is SQLite and the default storage is the local disk, so the
server itself needs nothing but Python 3.8+:

    pip install -r requirements.txt
    python3 -m host.app
    # open http://127.0.0.1:8788

The pinned list is short and split by need. `pypdf` and `pdfminer.six` are the
PDF reader; `pymysql` and `cryptography` are MySQL/MariaDB and the Azure
backends. Skip the install and EPUB, DOCX and web imports still work, because
those dependencies are loaded only when a PDF actually arrives, and a PDF import
without them says which package is missing instead of failing silently. See
*Backends* below for the database settings.

### What you see first

The first page is the way in. On a fresh server it reads:

> set up fireside
> create the first account. it will be the admin.

Pick a username and a password and you are reading. That is the whole setup for
one reader: no config file to write and no key to paste to get started. Voice
engines that need a key are added later, from inside the app.

## More than one reader

Leaving sign-ups open (the default) turns the same server into a household or a
hosted shelf. Each account gets its own library, its own playback position, and
its own storage allowance. The admin can close sign-ups once the household is
set up, add accounts by hand, grant storage, and revoke sessions and devices.

Sign-in has two shapes and a deployment can offer both:

- **Local account** - username and password, stored on this server as a salted
  PBKDF2 hash. This is the door a LAN box needs, and it is on by default; set
  `FIRESIDE_ALLOW_LOCAL=0` to hide it.
- **Social sign-in** - Google, GitHub, Microsoft, or SimpleLogin, configured by
  putting the client id and secret in the environment. A provider appears only
  when its credentials are present, so an unconfigured server offers none.
  Google requires an HTTPS redirect URI: a bare IP or plain http is rejected, so
  it needs a real hostname with TLS in front.

With `FIRESIDE_REQUIRE_AUTH=1` (automatic under `--hosted`) every route but the
page, `/health`, `/auth/providers` and `/me` needs a session.

## How it is built

fireside v2 is a **host plus plugins**, with a separate browser client. The host
owns the library, the render jobs, storage, the credit ledger, and the plugin
registry. It knows only the generic contracts; it never imports a provider by
name, and a plugin ships no HTML.

Three plugin contracts, all discovered from the filesystem under
`plugins/<kind>/<id>/`:

- **reader** - turns an input into chapters (EPUB, a plain web page, AO3);
- **speech** - lists voices and synthesizes audio (local, OpenAI-compatible,
  Azure, MiniMax);
- **storage** - keeps opaque blobs by content hash (local disk, Azure blob).

Drop a new `plugins/<kind>/<id>/` directory in and rebuild to add one. The full
map is in `docs/V2-ARCHITECTURE.md`; the contracts are in
`docs/SOURCE-CONTRACT.md`, `docs/PROVIDER-CONTRACT.md`, and `docs/STORAGE.md`.

The browser client is served by the host and talks to it over JSON. It imports
books, watches render jobs, **verifies each chapter by SHA-256 before a note
plays** (online or offline), resumes from one position per book, and hands
playback between devices (playing on one stops the other).

### Run on unraid (or any Docker host)

Use the Compose Manager plugin: add this repo as a stack, set the host port to
8788, and point the `data` mount at a share under `/mnt/user/appdata/fireside`.
The Community Applications template is at `templates/fireside.xml`. Unraid
creates the data directory as root and fireside runs unprivileged, so on first
start the app cannot create its SQLite file; the container entrypoint handles
that for you (it starts as root only long enough to make the data directory
writable, then drops back to the app user). Set `PUID`/`PGID` to own the files
yourself instead.

## Voice engines

| engine  | key | notes |
|---------|-----|-------|
| local   | none | Offline tone, for testing the flow. Not speech. |
| openai  | none (your server) | Any OpenAI-compatible `/v1/audio/speech` you host (Kokoro, Speaches, ...). |
| azure   | Azure Speech key + region | Hundreds of neural voices across accents and styles. |
| minimax | key + group id | MiniMax T2A. |

Keys are entered in the app and scoped to the connection that uses them. Nothing
leaves your machine except the text you ask to be spoken, to the provider you
picked, and only for the connection you chose.

## Save the audio

- **Render all chapters** works through the whole book in the background and
  reports progress chapter by chapter. It skips anything already rendered, so
  pressing it twice costs nothing.
- **Save one file** stitches the chapters into a single audio file for a phone
  or a podcast app that should just play straight through.
- **Save zip** hands back one file per chapter, numbered, in a folder named
  after the book.

Both are built once and cached next to the audio, so a second download is
instant. The chapter bytes underneath are plain `.mp3` (Azure, MiniMax), `.wav`
(the offline tone), or whatever the OpenAI-compatible engine returns, so you can
also just copy the folder.

## Backends

State lives in SQLite under `FIRESIDE_DATA` by default: nothing to install, one
file to back up. Point it at MySQL or MariaDB when the data should live
somewhere else:

    FIRESIDE_DB_TYPE=mysql
    FIRESIDE_DB_CONN=user:password@host:3306/fireside

The engine and the connection are independent variables; leave both unset to
stay on the default SQLite file. Storage defaults to the local disk and can be
swapped for Azure blob through the storage plugin. Both defaults together are a
fully functional server: every extra backend is optional.

## The old single-file reader

`server.py` is the v1 app: the whole thing in one process, no accounts, one
bookshelf for whoever connects. It still runs (`python3 server.py`, then open
`http://127.0.0.1:8765`), and the double-click launchers (`start_reader.bat`,
`start_reader.command`, `start_reader.sh`) still start it, for when you just want
to turn an EPUB into audio on your own machine with no accounts at all. New work
goes into the host above.

## Notes and limits

- Books are keyed by content hash, so re-importing the same book reuses its
  already-rendered audio.
- Rendering sends chapter text to the provider in ~3.5k-character pieces and
  joins the audio. An imported EPUB or a public, plain-HTML page parses;
  JavaScript-only pages and login-gated pages do not. AO3 works through
  **Download > EPUB**, which happens in your own browser and so also handles
  login-gated works.
- The `local` engine caps at 4 minutes; it is a stand-in.

## Provenance

Agent-authored, human-owned. The commit history is linear, one change per commit,
and nothing has been rewritten since the first public push. See [AUTHORS](AUTHORS)
for the authorship/ownership split and how the history proves it.
