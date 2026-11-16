"""Starting a render and the notices about what happened while you were away.

A mixin of host.app.App: its methods are the handlers the route tables name,
moved here verbatim so no one file holds the whole server. They use the App's
own state (self.conn, self.host, ...) and each other through self.
"""

import threading
from host import accounting, allowance, auth, connections, credentials, db, grants, jobs, library, notices, pron, renderjob_runner
from host.appsupport import _swallowed


class RenderRoutes:
    # ---- render ---------------------------------------------------------------

    def render(self, q, body):
        user = self._uid(q, body)
        return self._start_render(
            user, body["book"],
            cid=body.get("connection_id") or "",
            provider=body.get("provider", ""),
            voice=body.get("voice", ""),
            voice_label=body.get("voice_label", ""),
            model=body.get("model", ""),
            speed=float(body.get("speed", 1.0) or 1.0),
            only=body.get("chapters"),
            client_cred=body.get("cred"))

    def _start_render(self, user, book_id, cid="", provider="", voice="", model="",
                      speed=1.0, only=None, voice_label="", client_cred=None):
        cred = None
        if cid:
            c = connections.get(self.conn, cid)
            if not connections.may_use(c, user, auth.is_admin(self.conn, user)):
                return 403, {"error": "no such connection"}
            cred = connections.resolve(self.conn, user, cid)
            if cred is None:
                return 403, {"error": "connection is switched off"}
            provider = c["provider"]
        if provider not in self.host.speech:
            return 400, {"error": "unknown provider"}
        if not (voice or "").strip():
            # An empty voice is not "the default one": every speech provider has
            # its own idea of a default, so a render that arrives without a voice
            # produces audio nobody chose. The dialog can send this while its
            # voice list is still loading, and the result looks like a bug in the
            # book rather than a missing choice, so it is refused here where any
            # client hears it.
            return 400, {"error": "no voice chosen",
                         "detail": "pick a voice first; the list may still be loading"}
        if cred is None:
            cred = client_cred or credentials.get_credential(self.conn, user, provider)
        if self.rate_limiter is not None and not self.rate_limiter.allow(user):
            return 429, {"error": "rate limited"}
        speech = self.host.speech[provider].obj
        rows = library.chapters(self.conn, book_id)
        est = sum(c["chars"] or 0 for c in rows)
        todo = [c["idx"] for c in rows]
        if only is not None:
            todo = [i for i in todo if i in set(only)]
        todo_chars = sum(c["chars"] or 0 for c in rows if c["idx"] in set(todo))
        est_bytes = jobs.estimate(todo_chars, provider)["audio_bytes"]
        if not self._storage_ok(user, est_bytes):
            return 507, {"error": "not enough storage",
                         "detail": self._no_room(user, "this render", est_bytes)}
        sale = accounting.sale_connection_matches(self.sale_connection, provider, voice,
                                                  model, cid)
        # What this run actually has to synthesise: a chapter already voiced in
        # this exact voice (and rules) is a cache hit and costs the buyer
        # nothing. The whole-book estimate is the storage question, not the
        # money question, so the two are kept apart.
        rules = pron.for_book(pron.load(self.conn, user), book_id,
                              pron.book_author(self.conn, book_id),
                              pron.book_series(self.conn, book_id))
        fresh = jobs.stale_book_chars(self.conn, self.storage, speech, book_id, todo,
                                      rules, voice, model, speed)
        res_id = None
        if sale and not grants.credits_unlimited(self.conn, user):
            quote = speech.quote({"chars_est": fresh, "cred": cred, "sale": True})
            if not getattr(quote, "eligible", True):
                return 402, {"error": "not eligible",
                             "reason": getattr(quote, "refusal_reason", "")}
            price = float(getattr(quote, "price", 0.0) or 0.0)
            if price > 0:
                allowance.welcome(self.conn, user)
                allowance.due(self.conn, user)
                try:
                    res_id = accounting.reserve(self.conn, user, price,
                                                getattr(quote, "currency", "USD"))
                except accounting.Insufficient as e:
                    # The numbers travel with the refusal. The two words on their
                    # own leave a reader with no idea how far short they are, and
                    # the screen can only print what it is sent.
                    return 402, {"error": "insufficient balance",
                                 "needed": price,
                                 "available": accounting.available(self.conn, user),
                                 "detail": str(e)}
        jid = jobs.start_job(self.conn, user, book_id, speech.id, voice, model,
                             total=len(todo), status="queued",
                             connection_id=cid, speed=speed, only=only)
        if res_id:
            # The row carries what a worker in another process needs to settle
            # this charge: the reservation and the characters it quoted.
            jobs.set_reservation(self.conn, jid, res_id, fresh)
        opts = {"model": model, "speed": speed}

        def _work():
            # A render can run for minutes; it gets its own DB connection so it
            # never blocks the request thread and the client can poll /job.
            c = db.connect(self.db_path)
            try:
                jobs.render_book(c, self.storage, speech, user, book_id, voice,
                                 model, speed, cred=cred, opts=opts, only=only,
                                 job_id=jid, voice_label=voice_label,
                                 plugins_root=self.host.plugins_root)
                if sale and res_id:
                    # Charge only the characters this run actually synthesised:
                    # what is still stale after the run is what was never voiced
                    # (a chapter that was already cached, or one that failed).
                    # Shared with the out-of-process render job, which settles
                    # the same reservation from its own process.
                    accounting.settle_run(c, self.storage, speech, book_id, todo,
                                          rules, voice, model, speed, res_id,
                                          fresh, cred, jid)
                self._notice_run(c, user, book_id, jid)
            finally:
                c.close()

        # Out of process when this box can do it (a Container Apps Job on the
        # hosted deployment, a detached worker anywhere else), because a thread
        # dies with the request process. In process only as the fallback, and
        # then only one of the two runs: start() says whether something took it.
        mode = renderjob_runner.start(jid)
        if mode:
            jobs.set_worker(self.conn, jid, "worker")
        else:
            threading.Thread(target=_work, daemon=True).start()
        return 200, {"job_id": jid, "total": len(todo)}

    def _notice_run(self, c, user, book_id, jid):
        """Tell the bell how a run ended. The job row is the record; this reads
        it back rather than guessing, because render_book reports failure by
        writing a status, not by raising. A cancelled run is the reader's own
        doing and gets no notice."""
        try:
            j = jobs.job(c, jid) or {}
            st = j.get("status")
            if st not in ("done", "error"):
                return
            r = c.execute("SELECT title FROM books WHERE id=?", (book_id,)).fetchone()
            title = ((r["title"] if r else "") or "").strip() or "your book"
            route = "#/play/" + str(book_id)
            if st == "done":
                notices.notify(c, user, "job", "%s is ready to play." % title,
                               route=route, key="run:" + jid)
            else:
                notices.notify(c, user, "job", "Could not convert %s. %s"
                               % (title, (j.get("error") or "").strip()[:240]),
                               route=route, key="run:" + jid)
        except Exception as e:             # a notice never costs a finished run
            _swallowed("run notice", e)

    # ---- notifications: what happened while you were elsewhere ---------------
    def notices_list(self, q, body):
        """This user's notices, newest first. The unread count travels with the
        list so the bell can badge itself without a second request."""
        uid = self._uid(q, body)
        if not uid:
            return 401, {"error": "auth required"}
        try:
            n = max(1, min(50, int(((q or {}).get("n") or ["20"])[0])))
        except (TypeError, ValueError):
            n = 20
        return 200, {"items": notices.list_for(self.conn, uid, n),
                     "unread": notices.unread(self.conn, uid)}

    def notices_read(self, q, body):
        """Mark notices read: the listed ids, or all of them."""
        uid = self._uid(q, body)
        if not uid:
            return 401, {"error": "auth required"}
        body = body or {}
        ids = body.get("ids")
        unread = notices.mark_read(self.conn, uid, ids if isinstance(ids, list) else None)
        return 200, {"ok": True, "unread": unread}
