"""Disambiguation and the model pass: questions asked before audio, never after.

A mixin of host.app.App: its methods are the handlers the route tables name,
moved here verbatim so no one file holds the whole server. They use the App's
own state (self.conn, self.host, ...) and each other through self.
"""

import threading
from host import auth, connections, db, disambig, library, pron, reading
from host.appsupport import _detail, _stale_claim


class DisambigRoutes:
    # ---- disambiguation: ask before audio, never after ---------------------

    def _disambig(self, body, q, uid):
        """Resolve which disambiguator and which credential this request runs on.

        A connection named `disambig:<id>` is kept apart from the voice servers
        because they are usually different machines: the llama box that reads
        and the Kokoro box that speaks. Returns (plugin, cred) or (None, error).
        """
        pid = body.get("provider") or q.get("provider") or ""
        cid = body.get("connection_id") or q.get("connection_id") or ""
        cred = body.get("cred") or {}
        if cid:
            c = connections.get(self.conn, cid)
            if not c or not connections.may_use(
                    c, uid, auth.is_admin(self.conn, uid)):
                return None, (403, {"error": "no such connection"})
            resolved = connections.resolve(self.conn, uid, cid)
            if resolved is None:
                return None, (403, {"error": "connection is switched off"})
            pid, cred = c["provider"].split(":", 1)[-1], resolved
        if not pid:
            pid = next(iter(self.host.disambig), "")
        p = self.host.disambig.get(pid)
        if p is None:
            return None, (400, {"error": "unknown disambiguator"})
        return (p.obj, cred), None

    def disambig_models(self, q, body):
        """What the configured server can run, with prices where we have them.
        An empty list is 'unknown', never 'free'."""
        uid = self._uid(q, body)
        got, err = self._disambig(body, q, uid)
        if err:
            return err
        obj, cred = got
        try:
            models = obj.list_models(cred)
        except Exception:
            models = []
        return 200, {"models": models or []}

    def disambig_quote(self, q, body):
        """Price one pass for this text on this model. The plugin is the
        authority; the host only carries the number."""
        uid = self._uid(q, body)
        got, err = self._disambig(body, q, uid)
        if err:
            return err
        obj, cred = got
        ctx = {"text": body.get("text") or "", "model": body.get("model") or ""}
        qt = obj.quote(ctx)
        out = {"chars_est": qt.chars_est, "price": qt.price,
               "currency": qt.currency, "eligible": qt.eligible,
               "authoritative": qt.authoritative,
               "refusal_reason": qt.refusal_reason}
        return 200, {"quote": out}

    def disambig_scan(self, q, body):
        """The pass itself: text in, the questions that must be answered before
        any audio exists, out. A server that is down is an error, not an empty
        list, because an empty list silently voices the wrong reading."""
        uid = self._uid(q, body)
        got, err = self._disambig(body, q, uid)
        if err:
            return err
        obj, cred = got
        try:
            questions = obj.scan(body.get("text") or body.get("sentences") or [],
                                 body.get("model") or "", cred)
        except Exception as e:
            return 502, {"error": "scan failed",
                        "detail": _detail(e, "the reader could not read that text")}
        return 200, {"questions": questions or []}

    # ---- the model pass: suggestions, never settings ----------------------
    def _disambig_plugin(self):
        """The disambiguator this host actually loaded. Discovery happens once,
        at startup; asking the default plugin directory again here could load a
        second copy that the host never checked the contract against."""
        for p in self.host.disambig.values():
            if getattr(p.obj, "decide", None):
                return p.obj
        return None

    def _own_book(self, uid, book_id):
        """This account owns this book, or None. Every model-pass route goes
        through here: a book id is not a permission."""
        bk = library.get_book(self.conn, book_id) or {}
        if not bk or bk.get("user_id") != uid:
            return None
        return bk

    def _empty_model_state(self):
        return {"book": "", "available": disambig.configured(), "optin": False,
                "status": "", "words": 0, "rules": [], "note": "",
                "updated_at": "", "stale": None}

    def _model_pass_on(self, book_id, user):
        """Allow the pass on one book, and start it now."""
        disambig.set_optin(self.conn, book_id, True)
        return self._start_model_pass(book_id, user)

    def _start_model_pass(self, book_id, user):
        """Run the pass on this book in its own thread, off the request.

        The row, not a job, is the status: the pass belongs to the book and the
        page already polls the book. A second click while one is running is
        ignored rather than paid for twice.
        """
        if not disambig.configured():
            return None
        st = disambig.state(self.conn, book_id) or {}
        if st.get("status") == "running" and not _stale_claim(st.get("updated_at")):
            return None
        disambig._save(self.conn, user, book_id, "running", st.get("rules") or [],
                       st.get("note") or "")

        p = self._disambig_plugin()

        def _work():
            c = db.connect(self.db_path)
            try:
                disambig.scan(c, self.storage, user, book_id,
                              run=getattr(p, "decide", None))
            except Exception as e:
                try:
                    disambig._save(c, user, book_id, "failed", [],
                                   "the pass could not finish: " + str(e)[:200])
                except Exception:
                    pass
            finally:
                c.close()

        threading.Thread(target=_work, daemon=True).start()
        return disambig.state(self.conn, book_id)

    def book_model_get(self, q, body):
        """What the model pass did on this book, and what has gone stale since.

        `stale` is the question Chris asked for (11-11): the rules a chapter was
        voiced with are stamped on the rendition, so the host can say which
        chapters predate the reader's latest word changes without re-reading
        anything by hand.
        """
        uid = self._uid(q, body)
        if not uid:
            # A page with no session yet asks this on load. An empty state is the
            # honest answer; a 401 here would read as an error in the book.
            return 200, self._empty_model_state()
        bid = q.get("book") or ""
        # A route can name a book that is not here (a dead link, a notice whose
        # book was deleted). That is "nothing to say about it", not a refusal:
        # a 403 on a page load is an error nobody can act on.
        if not library.get_book(self.conn, bid):
            out = self._empty_model_state()
            out["book"] = bid
            return 200, out
        if not self._own_book(uid, bid):
            return 403, {"error": "no such book"}
        st = disambig.state(self.conn, bid) or {}
        out = {"book": bid, "available": disambig.configured(),
               "optin": disambig.opted_in(self.conn, bid),
               "status": st.get("status") or "", "words": st.get("words") or 0,
               "rules": st.get("rules") or [], "note": st.get("note") or "",
               "updated_at": st.get("updated_at") or ""}
        try:
            im = pron.impact(self.conn, self.storage, uid, bid,
                             split_for=self._speech_split)
            out["stale"] = {"chapters": im.get("chapters_affected") or 0,
                            "est_seconds": im.get("est_seconds") or 0,
                            "voice": ((im.get("books") or [{}])[0].get("renditions")
                                      or [{}])[0].get("voice_label", "") if im.get("books") else ""}
        except Exception:
            # Not knowing is not "nothing changed". The page says so in words.
            out["stale"] = None
        return 200, out

    def book_model_set(self, q, body):
        """scan | apply | discard | off. Each one is the reader's own hand."""
        uid = self._uid(q, body)
        if not uid:
            return 401, {"error": "auth required"}
        bid = body.get("book") or ""
        if not self._own_book(uid, bid):
            return 403, {"error": "no such book"}
        action = (body.get("action") or "scan").strip().lower()
        if action == "scan":
            if not disambig.configured():
                return 400, {"error": "no model box is set up"}
            disambig.set_optin(self.conn, bid, True)
            st = self._start_model_pass(bid, uid)
            return 200, {"book": bid, "status": (st or {}).get("status", "running"),
                         "optin": True}
        if action == "apply":
            st, err = disambig.apply(self.conn, uid, bid)
            if err:
                return 400, {"error": err}
            return 200, {"book": bid, "status": (st or {}).get("status", ""),
                         "words": (st or {}).get("words", 0), "optin": True}
        if action == "discard":
            st = disambig.discard(self.conn, uid, bid)
            return 200, {"book": bid, "status": (st or {}).get("status", ""),
                         "optin": disambig.opted_in(self.conn, bid)}
        if action == "off":
            disambig.set_optin(self.conn, bid, False)
            return 200, {"book": bid, "optin": False,
                         "status": (disambig.state(self.conn, bid) or {}).get("status", "")}
        return 400, {"error": "unknown action"}
