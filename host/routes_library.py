"""The library: import, covers, details, series, tags and book deletion.

A mixin of host.app.App: its methods are the handlers the route tables name,
moved here verbatim so no one file holds the whole server. They use the App's
own state (self.conn, self.host, ...) and each other through self.
"""

import base64
import time
import urllib.error
import zipfile
from host import billing, bookmeta, connections, contracts, credentials, disambig, draft as drafts, example, grants, jobs, library, netguard, playback, quota, stats, tags as tagmod
from host.egress import fetch as _fetch
from host.appsupport import _detail, _swallowed, _truthy


class LibraryRoutes:
    # ---- library --------------------------------------------------------------

    def library_list(self, q, body):
        return 200, library.list_books(self.conn, self._uid(q, None))

    def preview_import(self, q, body):
        """Read the file, decide nothing, write nothing.

        The user drops a PDF, waits for the parse, and looks at what the machine
        thinks the chapters and paragraphs are before a single character is
        charged. Nothing touches the shelf here, so a bad parse costs no storage
        and leaves no half-book behind. Duplicate detection still runs, because
        finding out after the preview that you already own this book is worse
        than finding out at the start.
        """
        user = self._uid(q, body)
        req = {"kind": body.get("kind", "url"), "url": body.get("url"),
               "filename": body.get("filename"),
               "data": base64.b64decode(body["data_b64"]) if body.get("data_b64") else None}
        plugin = self._pick_reader(req)
        if plugin is None:
            return 400, self._no_reader(req)
        cred = self._source_cred(user, plugin.id, body.get("cred"))
        try:
            book = plugin.obj.read(req, self._make_fetch(plugin.obj, cred), cred)
        except contracts.Refused as e:
            return e.status, self._refusal(e, user, plugin)
        except Exception as e:
            failed = self._import_error(e)
            if failed is None:
                raise
            return failed
        parsed = (getattr(book, "meta", None) or {}).get("draft")
        if not parsed:
            # A reader that does not produce a block-level draft (epub, ao3, web)
            # gets no preview: inventing one would mean re-parsing its text here
            # and the two parsers would disagree.
            return 200, {"preview": None, "source": plugin.id,
                         "title": getattr(book, "title", ""),
                         "chapters": len(getattr(book, "chapters", [])),
                         "detail": "this reader imports directly; no preview"}
        out = drafts.draft_summary(parsed, plugin.id)
        existing = library.find_same(self.conn, user,
                                     getattr(book, "meta", None) or {}, plugin.obj)
        if existing:
            held = library.get_book(self.conn, existing) or {}
            out["existing"] = {"book_id": existing, "title": held.get("title") or ""}
        return 200, {"preview": out}

    def import_book(self, q, body):
        user = self._uid(q, body)
        req = {"kind": body.get("kind", "url"), "url": body.get("url"),
               "filename": body.get("filename"),
               "data": base64.b64decode(body["data_b64"]) if body.get("data_b64") else None}
        plugin = self._pick_reader(req)
        if plugin is None:
            return 400, self._no_reader(req)
        cred = self._source_cred(user, plugin.id, body.get("cred"))
        try:
            book = plugin.obj.read(req, self._make_fetch(plugin.obj, cred), cred)
        except contracts.Refused as e:
            # A plugin that can read the file but not turn it into a book says
            # so in its own words. That is a 422 with a reason, not a 500.
            return e.status, self._refusal(e, user, plugin)
        except Exception as e:
            failed = self._import_error(e)
            if failed is None:
                raise
            return failed
        # Whatever identity the reader named, this account either already holds
        # that work or it does not. Importing again is a replace, not a second
        # copy: two of the same book on one shelf is a mistake, not a collection.
        existing = library.find_same(self.conn, user,
                                     getattr(book, "meta", None) or {}, plugin.obj)
        if existing and _truthy(body.get("keep_both")):
            existing = None
        elif existing and not _truthy(body.get("replace")):
            held = library.get_book(self.conn, existing) or {}
            chs = library.chapters(self.conn, existing)
            return 409, {
                "error": "you already have this book",
                "detail": "this is already on your shelf as \"%s\""
                          % (held.get("title") or ""),
                "existing": {"book_id": existing,
                             "title": held.get("title") or "",
                             "chapters": len(chs),
                             "rendered": sum(1 for c in chs if c["audio_ref"])}}
        incoming = self._book_bytes(book)
        # A replace only needs room for the difference: the chapter text and
        # audio it is about to displace are already counted against this plan.
        already = library.shelf_bytes(self.conn, existing) if existing else 0
        plan = (billing.get(self.conn, user) or {}).get("plan") or ""
        blocks = billing.blocks(self.conn, user)
        # The same gate every other write uses, so an admin storage grant lifts
        # an import too. Asking quota.check_room directly here skipped the grant
        # and refused a book to an account whose limit was already lifted.
        if not self._storage_ok(user, max(0, incoming - already)):
            left = max(0, quota.allowance_bytes(plan, blocks)
                       - quota.usage(self.conn, self.storage, user)["own_bytes"])
            return 507, {"error": "not enough storage",
                         "detail": "this book needs about %d bytes; %d left in your plan"
                                   % (max(0, incoming - already), left)}
        # A file's own subject list becomes opf tags on the shelf, so the
        # import is the one place a book's metadata arrives already filterable.
        subjects = (getattr(book, "meta", None) or {}).get("subjects") or []
        if existing:
            swapped = library.replace_book(self.conn, self.storage, existing, book)
            if subjects:
                tagmod.subjects_into_tags(self.conn, user, existing, subjects)
            self._file_meta(existing, book)
            self._maybe_cover(existing, book)
            if _truthy(body.get("model_pass")):
                self._model_pass_on(existing, user)
            out = {"book_id": existing, "title": getattr(book, "title", ""),
                   "chapters": len(getattr(book, "chapters", [])),
                   "replaced": True}
            out.update(swapped)
            return 200, out
        bid = library.add_book(self.conn, self.storage, user, book,
                               tags_from=subjects)
        self._file_meta(bid, book)
        self._maybe_cover(bid, book)
        # The reader ticked the box on this import, so the pass is allowed on
        # this book and no other. It starts now, in its own thread: a model over a
        # whole book is minutes, and an import is a click.
        if _truthy(body.get("model_pass")):
            self._model_pass_on(bid, user)
        return 200, {"book_id": bid, "title": getattr(book, "title", ""),
                     "chapters": len(getattr(book, "chapters", [])),
                     "model_pass": disambig.opted_in(self.conn, bid)}

    def _refusal(self, e, user, plugin):
        """A plugin's refusal, made useful before it reaches the page.

        A series page is refused because it is several books, and the useful
        answer is the list, not the sentence. Whichever of them this account
        already holds is marked here, from the local shelf, so the picker can
        say so without opening any of them.
        """
        out = e.payload()
        ser = out.get("series")
        if isinstance(ser, dict) and isinstance(ser.get("works"), list):
            for w in ser["works"]:
                if not isinstance(w, dict):
                    continue
                hit = library.find_same(self.conn, user,
                                        {"url": w.get("url")}, plugin.obj)
                if hit:
                    held = library.get_book(self.conn, hit) or {}
                    w["existing"] = {"book_id": hit,
                                     "title": held.get("title") or ""}
        return out

    def import_series(self, q, body):
        """Import the works a person picked out of a series page.

        The list comes from the page, not from the request: this re-reads the
        page through the same reader and imports only URLs that page links to,
        so a picker can never become a way to fetch an arbitrary address. Each
        work is imported by the ordinary import path, one at a time, so every
        rule that applies to a single book (duplicate, quota, replace) applies
        here unchanged; what fails is reported per work instead of failing the
        batch. The books that land are put in one series container, in page
        order, so the shelf reads in the author's order rather than by import
        time.
        """
        user = self._uid(q, body)
        page_url = (body.get("url") or "").strip()
        req = {"kind": "url", "url": page_url}
        plugin = self._pick_reader(req)
        if plugin is None:
            return 400, self._no_reader(req)
        cred = self._source_cred(user, plugin.id, body.get("cred"))
        reader = plugin.obj
        try:
            found = reader.series(req, self._make_fetch(reader, cred), cred)
        except contracts.Refused as e:
            return e.status, self._refusal(e, user, plugin)
        except Exception as e:
            failed = self._import_error(e)
            if failed is None:
                raise
            return failed
        if not isinstance(found, dict) or not found.get("works"):
            return 422, {"error": "not a series page",
                         "detail": "that page does not list works of its own"}
        works = found["works"]
        picked = [str(u) for u in (body.get("picks") or [])]
        if not picked:
            return 400, {"error": "nothing picked",
                         "detail": "choose at least one work to pull"}
        by_url = {w["url"]: w for w in works if isinstance(w, dict)}
        # Page order, not request order: the shelf reads in the author's order
        # because this is a series, not a pile of books that arrived together.
        chosen = [w for w in works if isinstance(w, dict) and w["url"] in picked]
        unknown = [u for u in picked if u not in by_url]
        if not chosen:
            return 400, {"error": "nothing picked",
                         "detail": "none of those are on that page",
                         "unknown": unknown}
        done, failed = [], []
        for w in chosen:
            row = {"url": w["url"], "title": w.get("title") or "",
                   "words": w.get("words")}
            # One broken work must not take the batch down with it: a page the
            # site changed, a story that is now a login wall. The rest still land.
            try:
                status, out = self.import_book(
                    q, dict(body, kind="url", url=w["url"]))
            except Exception as e:                    # noqa: BLE001
                row.update({"error": "could not read this work",
                            "detail": _detail(e, "that page would not open")})
                failed.append(row)
                continue
            if status == 200 and out.get("book_id"):
                row.update({"book_id": out["book_id"],
                            "chapters": out.get("chapters"),
                            "replaced": bool(out.get("replaced"))})
                done.append(row)
            elif status == 409 and (out.get("existing") or {}).get("book_id"):
                # already on the shelf: not a failure, and still this series'
                row.update({"book_id": out["existing"]["book_id"],
                           "already": True})
                done.append(row)
            else:
                row.update({"error": out.get("error") or "import failed",
                            "detail": out.get("detail") or ""})
                failed.append(row)
        name = " ".join(str(found.get("title") or "").split())[:120]
        container = None
        if name and done:
            container = self._series_container(user, name,
                                              [r["book_id"] for r in done])
        return 200, {"title": name or "Series", "series": container,
                     "imported": done, "failed": failed,
                     "unknown": unknown}

    def _series_container(self, user, name, book_ids):
        """The series these books belong to, by name: reused if it exists."""
        sid = None
        for s in library.list_series(self.conn, user):
            if (s.get("name") or "").casefold() == name.casefold():
                sid = s["id"]
                break
        if not sid:
            sid = library.create_series(self.conn, user, name)
        if not sid:
            return None
        for bid in book_ids:
            library.set_book_series(self.conn, user, bid, sid)
        return {"id": sid, "name": name, "books": len(book_ids)}

    def _example_voice(self):
        """The free voice the example is pre-rendered in: the sale connection
        and the voice named on it. None when no sale connection is configured,
        in which case the example is simply a free book to add, not a free
        listen."""
        spec = self.sale_connection or {}
        cid = spec.get("connection_id")
        if not cid:
            return None
        c = connections.get(self.conn, cid)
        if not c:
            return None
        return {"connection_id": cid, "provider": c["provider"],
                "voice": spec.get("voice") or example.DEFAULT_VOICE,
                "voice_label": c["label"] or ""}

    def add_example_book(self, q, body):
        """Put the bundled example book on this user's shelf.

        A click, not an upload: the text is bundled, so the same bytes land on
        every shelf and a single pre-render serves them all. Adding it twice
        finds the copy already there. With `render` set and a sale connection
        configured, it also starts the free voice; because the text and voice
        are already cached, that costs the reader nothing."""
        user = self._uid(q, body)
        if not user:
            return 401, {"error": "auth required"}
        bid = library.find_by_meta(self.conn, user, "example", example.EXAMPLE_MARK)
        made = False
        if not bid:
            book = example.book()
            incoming = self._book_bytes(book)
            if not self._storage_ok(user, incoming):
                return 507, {"error": "not enough storage",
                             "detail": self._no_room(user, "the example", incoming)}
            bid = library.add_book(self.conn, self.storage, user, book)
            self._fill_example_details(bid)
            self._maybe_cover(bid, book)
            made = True
        else:
            # A copy added before the bundled record was complete keeps its
            # blanks. Fill those, and only those: a field the reader typed stays
            # their text, and a cover they chose stays their cover.
            if self._fill_example_details(bid):
                self._maybe_cover(bid, example.book())
        b = library.get_book(self.conn, bid) or {}
        out = {"book_id": bid, "title": b.get("title", ""),
               "chapters": len(library.chapters(self.conn, bid)),
               "existing": not made,
               "example_voice": self._example_voice()}
        if body.get("render") and out["example_voice"]:
            ev = out["example_voice"]
            st, r = self._start_render(user, bid, cid=ev["connection_id"],
                                       provider=ev["provider"], voice=ev["voice"],
                                       voice_label=ev["voice_label"])
            out["render_status"] = st
            out["render"] = r
        return 200, out

    def _fill_example_details(self, book_id):
        """Write the bundled record onto an example copy, blank fields only.

        Returns True when something was written, so the caller can also put the
        cover on. Never overwrites a field the reader can edit: the example is a
        starting point, not an authority over their shelf."""
        b = library.get_book(self.conn, book_id) or {}
        fill = {k: v for k, v in example.details().items()
                if not (b.get(k) or "").strip()}
        if not fill:
            return False
        library.set_details(self.conn, book_id, **fill)
        return True

    @staticmethod
    def _book_bytes(book):
        """Upper-bound bytes an import would add: every chapter's text plus the
        reader's cover, if any. Not deduped against blobs the user already holds,
        so the gate may over-refuse; it must never over-commit."""
        total = 0
        for ch in getattr(book, "chapters", None) or []:
            text = ch.text if isinstance(ch.text, str) else str(ch.text)
            total += len(text.encode("utf-8"))
        cover = getattr(book, "cover", None)
        if cover:
            total += len(cover)
        return total

    def _plan(self, user_id):
        return (billing.get(self.conn, user_id) or {}).get("plan") or ""

    def _storage_ok(self, user_id, nbytes):
        """True when `nbytes` of new storage fits this user's plan. Reads only;
        the caller refuses the write. An admin grant lifts the check entirely."""
        if grants.storage_unlimited(self.conn, user_id):
            return True
        return quota.check_room(self.conn, self.storage, user_id,
                                self._plan(user_id), nbytes,
                                billing.blocks(self.conn, user_id))

    def _no_room(self, user_id, what, nbytes):
        """The sentence a refused write gets. On free the honest next step is a
        plan, not a block button, and the screen no longer offers one there."""
        if self._plan(user_id) == quota.FREE_PLAN:
            return ("%s needs about %d bytes and the free plan has no room for "
                    "it. Storage comes with a plan; the wallet pays for "
                    "narration." % (what, nbytes))
        return "%s needs about %d bytes and your plan has no room for it" % (what, nbytes)

    # ---- cover art -----------------------------------------------------------
    # A book's cover is its own image when the reader handed one over (an epub
    # almost always carries it), otherwise it is fetched by ISBN. Both paths are
    # generic: the host has no branch on which reader produced the book.

    COVER_MAX_BYTES = bookmeta.COVER_MAX_BYTES

    def _file_meta(self, book_id, book):
        """Whatever the file itself stated about the book, written down.

        Only fields the file actually carried, and only author and series from
        explicit metadata: dc:creator, calibre:series. A reader's own edit wins,
        so a replacement import never overwrites what someone typed. Absent
        stays absent, which is what makes the scopes honest: a book with no
        author belongs to the unknown bucket rather than to a guess.
        """
        meta = getattr(book, "meta", None) or {}
        if not isinstance(meta, dict):
            return
        row = self.conn.execute("SELECT user_edited FROM books WHERE id=?",
                                (book_id,)).fetchone()
        if row and (row["user_edited"] or 0):
            return
        fields = {}
        for key, col in (("author", "author"), ("series", "series_name"),
                         ("series_index", "series_index"),
                         ("publisher", "publisher"), ("published", "published")):
            v = str(meta.get(key) or "").strip()
            if v:
                fields[col] = v[:255]
        if fields:
            library.set_details(self.conn, book_id, **fields)

    def _maybe_cover(self, book_id, book):
        data = getattr(book, "cover", None)
        mime = getattr(book, "cover_mime", "") or "image/jpeg"
        if not data:
            meta = getattr(book, "meta", None) or {}
            isbn = (meta.get("isbn") if isinstance(meta, dict) else "") or ""
            got = self._cover_by_isbn(isbn)
            if got:
                data, mime = got
        if not data:
            return
        try:
            library.set_cover(self.conn, self.storage, book_id, data, mime)
        except Exception as e:          # a cover is a nicety; never fail an import for it
            _swallowed("cover save", e)

    def _cover_by_isbn(self, isbn):
        """Fetch a cover for an ISBN from a keyless public source, or None."""
        return bookmeta.cover_by_isbn(isbn)

    def cover_get(self, q, body):
        got = library.cover(self.conn, self.storage, q.get("book", ""))
        if not got:
            return 404, {"error": "no cover"}
        data, mime = got
        return 200, {"_raw": data, "_mime": mime, "checksum": ""}

    def put_cover(self, q, body):
        book_id = body.get("book", "")
        book = library.get_book(self.conn, book_id)
        if not book:
            return 404, {"error": "no such book"}
        owner = book["user_id"]
        url = (body.get("url") or "").strip()
        if url:
            got = self._cover_from_url(url)
            if not got:
                return 400, {"error": "could not fetch an image from that URL"}
            data, mime = got
            if not self._storage_ok(owner, len(data)):
                return 507, {"error": "not enough storage",
                             "detail": self._no_room(owner, "this cover", len(data))}
            library.set_cover(self.conn, self.storage, book_id, data, mime)
            return 200, {"ok": True, "cover": library.get_book(self.conn, book_id)["cover"]}
        raw = body.get("data_b64") or ""
        try:
            data = base64.b64decode(raw) if raw else b""
        except Exception:
            return 400, {"error": "bad image data"}
        if not data:
            return 400, {"error": "no image"}
        if len(data) > self.COVER_MAX_BYTES:
            return 413, {"error": "image is too large"}
        if not self._storage_ok(owner, len(data)):
            return 507, {"error": "not enough storage",
                         "detail": self._no_room(owner, "this cover", len(data))}
        library.set_cover(self.conn, self.storage, book_id, data,
                          body.get("mime") or "image/jpeg")
        return 200, {"ok": True, "cover": library.get_book(self.conn, book_id)["cover"]}

    def clear_cover(self, q, body):
        book_id = body.get("book", "")
        if not library.get_book(self.conn, book_id):
            return 404, {"error": "no such book"}
        library.clear_cover(self.conn, book_id)
        return 200, {"ok": True, "cover": ""}

    def _owned_book(self, uid, book_id):
        """The book if this user holds it, else (None, an error response)."""
        b = library.get_book(self.conn, book_id)
        if not b:
            return None, (404, {"error": "no such book"})
        if b["user_id"] != uid:
            return None, (403, {"error": "not yours to remove"})
        return b, None

    def remove_renditions(self, q, body):
        """Remove one voice from a book, or every voice it has. The book and
        its text stay; only the named voice's audio goes (a shared copy on
        another shelf is kept)."""
        uid = self._uid(q, body)
        book_id = body.get("book", "")
        _b, err = self._owned_book(uid, book_id)
        if err:
            return err
        if jobs.active_for_book(self.conn, book_id):
            return 409, {"error": "a conversion for this book is still running",
                         "detail": "cancel it, then remove the voice"}
        voice = (body.get("voice") or "").strip()
        provider = (body.get("provider") or "").strip()
        model = (body.get("model") or "").strip()
        all_voices = bool(body.get("all")) or not voice
        out = library.remove_renditions(
            self.conn, self.storage, book_id,
            provider="" if all_voices else provider,
            voice="" if all_voices else voice,
            model="" if all_voices else model)
        return 200, {"ok": True, "removed": out["removed"], "freed": out["freed"],
                     "scope": "all" if all_voices else "voice",
                     "storage": quota.usage(self.conn, self.storage, uid)}

    def delete_book(self, q, body):
        """Remove a book from the shelf: its rows, then any blob no other shelf
        (or the render cache) still names. Its position, device stops and listen
        rows for this user go with it."""
        uid = self._uid(q, body)
        book_id = body.get("book", "")
        b, err = self._owned_book(uid, book_id)
        if err:
            return err
        if jobs.active_for_book(self.conn, book_id):
            return 409, {"error": "a conversion for this book is still running",
                         "detail": "cancel it, then delete the book"}
        title = b.get("title", "")
        out = library.delete_book(self.conn, self.storage, book_id)
        playback.discard_book(self.conn, uid, book_id)
        stats.discard_book(self.conn, uid, book_id)
        return 200, {"ok": True, "title": title, "freed": out["freed"],
                     "storage": quota.usage(self.conn, self.storage, uid)}

    BULK_DELETE_MAX = 500

    def delete_books(self, q, body):
        """Remove a picked set from the shelf in one call: the same removal as
        /book/delete, once per book, then one storage recount.

        A book this user does not hold, or one with a conversion still running,
        is left alone and named back in `kept` with its reason - one stuck job
        must not block the other twenty-nine. Nothing partial is hidden: the
        caller gets the count it actually removed."""
        uid = self._uid(q, body)
        ids = []
        for x in (body.get("books") or []):
            x = str(x or "").strip()
            if x and x not in ids:
                ids.append(x)
        if not ids:
            return 400, {"error": "no books named"}
        if len(ids) > self.BULK_DELETE_MAX:
            return 413, {"error": "too many books in one call",
                         "detail": "remove at most %d at a time" % self.BULK_DELETE_MAX}
        removed, kept = [], []
        refs = []
        for book_id in ids:
            b, err = self._owned_book(uid, book_id)
            if err:
                kept.append({"book": book_id, "title": "",
                             "reason": err[1].get("error", "not yours to remove")})
                continue
            if jobs.active_for_book(self.conn, book_id):
                kept.append({"book": book_id, "title": b.get("title", ""),
                             "reason": "a conversion for this book is still running"})
                continue
            refs.extend(library.delete_book_rows(self.conn, book_id))
            playback.discard_book(self.conn, uid, book_id)
            stats.discard_book(self.conn, uid, book_id)
            removed.append({"book": book_id, "title": b.get("title", "")})
        # one sweep for the whole set, after every row is gone: a blob only one
        # of the picked books named is freed here, and a blob one of them still
        # holds is kept. Doing it per book means the same lookups, twice.
        freed = library.gc_blobs(self.conn, self.storage, refs)
        return 200, {"ok": True, "deleted": len(removed), "books": removed,
                     "kept": kept, "freed": freed,
                     "storage": quota.usage(self.conn, self.storage, uid)}

    def _url_allowed(self, url):
        """True only for an http(s) URL whose host resolves to a public address.
        A cover URL is user input, so it must never reach this box's own
        network or a file on it."""
        return netguard.is_public_url(url)

    def _cover_from_url(self, url):
        return bookmeta.cover_from_url(url, allowed=self._url_allowed)

    def cover_auto(self, q, body):
        book_id = body.get("book", "")
        b = library.get_book(self.conn, book_id)
        if not b:
            return 404, {"error": "no such book"}
        isbn = b.get("isbn") or body.get("isbn") or (b.get("meta") or {}).get("isbn") or ""
        got = self._cover_by_isbn(isbn)
        if not got:
            return 404, {"error": "no cover found" + (" for that ISBN" if isbn else "")}
        data, mime = got
        library.set_cover(self.conn, self.storage, book_id, data, mime)
        return 200, {"ok": True, "cover": library.get_book(self.conn, book_id)["cover"]}

    # ---- book details ---------------------------------------------------------
    # An ISBN is the key to a book's public metadata. Given one, the settings
    # panel can fill in author, description, publisher and date without the
    # reader having typed any of it. OpenLibrary is keyless, so this works on a
    # fresh install. Nothing here overwrites the book's own title.

    def _get_json(self, url):
        return bookmeta.get_json(url)

    @staticmethod
    def _norm_isbn(isbn):
        return bookmeta.norm_isbn(isbn)

    @staticmethod
    def _valid_isbn(isbn):
        return bookmeta.valid_isbn(isbn)

    def _details_by_isbn(self, isbn):
        """Public metadata for an ISBN from OpenLibrary, or None."""
        return bookmeta.details_by_isbn(isbn, get_json=self._get_json)

    def fetch_details(self, q, body):
        """Look a book up by ISBN and store what we learn. An ISBN in the body
        is saved first (the panel just typed one); otherwise the stored one is
        used. The cover is fetched too, but only when the book has none."""
        book_id = body.get("book", "")
        b = library.get_book(self.conn, book_id)
        if not b:
            return 404, {"error": "no such book"}
        isbn = self._norm_isbn(body.get("isbn"))
        if isbn:
            library.set_details(self.conn, book_id, isbn=isbn)
            b = library.get_book(self.conn, book_id)
        else:
            isbn = b.get("isbn") or ""
        if not self._valid_isbn(isbn):
            return 400, {"error": "that does not look like an ISBN"}
        details = self._details_by_isbn(isbn)
        if not details:
            return 404, {"error": "no details found for that ISBN"}
        fields = {k: details[k] for k in
                  ("author", "publisher", "published", "description")
                  if details.get(k)}
        if not b.get("title") and details.get("title"):
            fields["title"] = details["title"]
        if fields:
            library.set_details(self.conn, book_id, **fields)
        if not b.get("cover"):
            got = self._cover_by_isbn(isbn)
            if got:
                try:
                    library.set_cover(self.conn, self.storage, book_id, got[0], got[1])
                except Exception as e:
                    _swallowed("cover save", e)
        return 200, {"ok": True, "book": self._book_payload(book_id)}

    def _book_payload(self, book_id):
        b = library.get_book(self.conn, book_id) or {}
        meta = b.get("meta") or {}
        return {"id": book_id, "title": b.get("title", ""),
                "author": b.get("author", ""), "cover": b.get("cover", ""),
                # where the work came from, so the play page can credit it: the
                # reader named both when it read the book, and this is only a
                # pass-through of that (an upload has neither).
                "url": meta.get("url", "") or "",
                "source": meta.get("source", "") or "",
                "isbn": b.get("isbn", ""), "publisher": b.get("publisher", ""),
                "published": b.get("published", ""),
                "description": b.get("description", ""),
                "user_edited": b.get("user_edited", 0),
                "series": library.series_of(self.conn, book_id),
                "tags": tagmod.for_user(self.conn, b.get("user_id") or "")
                              .get(book_id, [])}

    # ---- metadata + series ----------------------------------------------------
    # Two calls with a sharp line between them. /book/details looks facts up by
    # ISBN; /book/meta is the reader typing over them. Only the second raises
    # user_edited, which is what makes a later lookup stop being authoritative.

    def book_meta_set(self, q, body):
        book_id = body.get("book", "")
        b = library.get_book(self.conn, book_id)
        if not b or b.get("user_id") != self._uid(q, body):
            return 404, {"error": "no such book"}
        fields = {}
        for k in ("title", "author"):
            if k in body:
                fields[k] = " ".join(str(body[k] or "").split())[:255]
        if not fields:
            return 400, {"error": "nothing to save"}
        if "title" in fields and not fields["title"]:
            return 400, {"error": "a book needs a title"}
        library.set_user_details(self.conn, book_id, **fields)
        return 200, {"ok": True, "book": self._book_payload(book_id)}

    def book_read_set(self, q, body):
        """Mark a book read or unread. Nothing is charged and no text moves: it
        is the reader's own shelf state, which is why it lives on the book the
        reader owns and not on the work."""
        book_id = body.get("book", "")
        b = library.get_book(self.conn, book_id)
        if not b or b.get("user_id") != self._uid(q, body):
            return 404, {"error": "no such book"}
        library.set_read(self.conn, book_id, bool(body.get("read", True)))
        return 200, {"ok": True, "read": library.is_read(self.conn, book_id)}

    def series_list(self, q, body):
        return 200, {"series": library.list_series(self.conn, self._uid(q, body))}

    def series_save(self, q, body):
        """create / rename / delete. A series is a local container; deleting it
        never deletes a book."""
        uid = self._uid(q, body)
        op = (body.get("op") or "create").strip()
        if op == "create":
            sid = library.create_series(self.conn, uid, body.get("name"))
            if not sid:
                return 400, {"error": "a series needs a name"}
        elif op == "rename":
            if not library.rename_series(self.conn, uid, body.get("id", ""), body.get("name")):
                return 404, {"error": "no such series"}
        elif op == "delete":
            if not library.delete_series(self.conn, uid, body.get("id", "")):
                return 404, {"error": "no such series"}
        else:
            return 400, {"error": "unknown op"}
        return 200, {"ok": True, "series": library.list_series(self.conn, uid)}

    def book_series_set(self, q, body):
        """Put this book in a series (at the end) or take it out."""
        book_id = body.get("book", "")
        if not library.set_book_series(self.conn, self._uid(q, body), book_id,
                                       body.get("series", "")):
            return 404, {"error": "no such book or series"}
        return 200, {"ok": True, "book": self._book_payload(book_id)}

    def tags_get(self, q, body):
        """The shelf's vocabulary: every tag in use and how many books carry it.

        One call, because the filter chips are a list of words and their
        counts; asking per book would make the screen wait on the library.
        """
        user = self._uid(q, None)
        return 200, {"tags": [{"tag": t, "books": n}
                              for t, n in tagmod.counts(self.conn, user).items()]}

    def tags_set(self, q, body):
        """Add or remove one tag on one book.

        `op` is add or remove. Removal of a tag the file itself declared hides
        it rather than deleting it, so the next import cannot put it back.
        """
        user = self._uid(q, body)
        book_id = (body or {}).get("book", "")
        tag = (body or {}).get("tag", "")
        op = (body or {}).get("op", "add")
        if not user or not book_id:
            return 400, {"error": "book required"}
        if op == "remove":
            ok = tagmod.remove(self.conn, user, book_id, tag)
        else:
            got = tagmod.add(self.conn, user, book_id, tag,
                             source=(body or {}).get("source") or "user")
            ok = bool(got)
        return 200, {"ok": ok, "book": self._book_payload(book_id),
                     "tags": tagmod.for_user(self.conn, user).get(book_id, [])}

    def series_move(self, q, body):
        """Up or down one place inside the series. `dir` is -1 or 1."""
        series_id = body.get("series", "")
        try:
            step = int(body.get("dir", 0))
        except (TypeError, ValueError):
            step = 0
        moved = library.move_in_series(self.conn, self._uid(q, body), series_id,
                                      body.get("book", ""), step)
        return 200, {"ok": True, "moved": bool(moved),
                     "book": self._book_payload(body.get("book", ""))}

    @staticmethod
    def _no_reader(req):
        """Why nothing claimed this upload, in words a person can act on.

        "no reader matched" is true and useless. If the name says a format we
        do read, the file is the problem, not the format.
        """
        name = (req.get("filename") or "").lower()
        for ext, what in ((".pdf", "a PDF"), (".docx", "a Word document"),
                          (".epub", "an EPUB")):
            if name.endswith(ext):
                return {"error": "could not read this file",
                        "detail": "this upload does not open as %s. It may be "
                                  "truncated or renamed." % what}
        return {"error": "no reader matched",
                "detail": "nothing here reads that file type"}

    def _pick_reader(self, req):
        for p in self.host.readers:
            try:
                if p.obj.matches(req):
                    return p
            except Exception as e:      # one broken reader must not hide the rest
                _swallowed("reader %s matches()" % getattr(p, "id", "?"), e)
                continue
        return None

    def _import_error(self, e):
        """(status, body) for an import that failed on the user's input, else None.

        A link that cannot be fetched is the user's link failing, not the
        server: a refused address is theirs to fix (400) and an unreachable or
        erroring site is upstream (502). A file that is not a usable zip, or
        inflates past the member limit, is a 422. Anything else is a bug and
        stays a 500.
        """
        if isinstance(e, zipfile.BadZipFile):
            return 422, {"error": "that file is damaged, or too large once unpacked"}
        if isinstance(e, netguard.BlockedAddress):
            return 400, {"error": str(e)}
        if isinstance(e, urllib.error.HTTPError):
            return 502, {"error": "could not fetch that link (HTTP %d)" % e.code}
        if isinstance(e, (OSError, TimeoutError)):
            return 502, {"error": "could not reach that link"}
        if isinstance(e, RuntimeError) and str(e).startswith("HTTP "):
            return 502, {"error": "could not fetch that link (%s)"
                                  % str(e).split(" from ")[0]}
        return None

    def _make_fetch(self, o, cred):
        hosts = tuple(o.credential_hosts() or []) if callable(
            getattr(o, "credential_hosts", None)) else ()
        tr = o.transport() if callable(getattr(o, "transport", None)) else None

        def f(url, **kw):
            return _fetch(url, cred=cred, credential_hosts=hosts, transport=tr)
        return f

    def _source_cred(self, user, source_id, client_cred=None):
        """The credential a source reads with. The server holds it; a value the
        client sent for this source wins over a stored one (a fresh paste), and
        otherwise the stored value fills in. A reader's login never has to live
        in a browser."""
        cred = dict(client_cred or {})
        stored = credentials.get_credential(self.conn, user, "source:" + source_id)
        for k, v in (stored or {}).items():
            if v not in (None, ""):
                cred.setdefault(k, v)
        return cred
