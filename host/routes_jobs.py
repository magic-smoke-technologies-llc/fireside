"""Job status, progress, chapter text and audio.

A mixin of host.app.App: its methods are the handlers the route tables name,
moved here verbatim so no one file holds the whole server. They use the App's
own state (self.conn, self.host, ...) and each other through self.
"""

from host import auth, db, jobs, library, pron, reading


class JobRoutes:
    def job(self, q, body):
        j = jobs.job(self.conn, q.get("id", ""))
        return (200, j) if j else (404, {"error": "no job"})

    def jobs_list(self, q, body):
        """This user's recent conversions, newest first, so the client can show
        a queued or running one and let the reader cancel it after a reload."""
        uid = self._uid(q, body)
        return 200, {"jobs": jobs.list_for(self.conn, uid, limit=20)}

    def cancel_job(self, q, body):
        """Stop a queued or running render. The row is marked at once so the
        page sees it, and the worker is signalled so it unwinds before its next
        chapter. A job that already finished is reported, not restarted."""
        uid = self._uid(q, body)
        jid = body.get("job") or q.get("job") or q.get("id") or ""
        j = jobs.job(self.conn, jid)
        if not j:
            return 404, {"error": "no job"}
        if j["user_id"] != uid and not auth.is_admin(self.conn, uid):
            return 403, {"error": "not your job"}
        if j["status"] in ("done", "error", "cancelled"):
            return 200, {"cancel": False, "job": j, "reason": j["status"]}
        jobs.request_cancel(jid)
        self.conn.execute(
            "UPDATE jobs SET status='cancelled',error=?,updated_at=? WHERE id=?",
            ("cancelled by user", db.now(), jid))
        self.conn.commit()
        return 200, {"cancel": True, "job": jobs.job(self.conn, jid)}

    def estimate(self, q, body):
        """Pre-convert time and disk, before the user commits to a long job."""
        book_id = q.get("book", "")
        provider = q.get("provider", "")
        rows = library.chapters(self.conn, book_id)
        idxs = [c["idx"] for c in rows]
        only = q.get("chapters")
        if only:
            sel = set()
            for x in str(only).split(","):
                x = x.strip()
                if x:
                    try:
                        sel.add(int(x))
                    except ValueError:
                        pass
            idxs = [i for i in idxs if i in sel]
        chars = sum((c["chars"] or 0) for c in rows if c["idx"] in set(idxs))
        return 200, jobs.estimate(chars, provider)

    def progress(self, q, body):
        book_id = q.get("book", "")
        b = library.get_book(self.conn, book_id) or {}
        ch = library.chapters(self.conn, book_id)
        rends = {}
        for r in library.renditions(self.conn, book_id):
            rends.setdefault(r["idx"], []).append({
                "provider": r["provider"], "voice": r["voice"],
                "voice_label": r["voice_label"], "model": r["model"],
                "audio_ref": r["audio_ref"], "audio_bytes": r["audio_bytes"]})
        return 200, {"book": self._book_payload(book_id),
                     "chapters": [
            {"idx": c["idx"], "title": c["title"], "chars": c["chars"],
             "rendered": bool(c["audio_ref"]),
             "audio_ref": c["audio_ref"], "audio_bytes": c["audio_bytes"],
             "provider": c["provider"], "voice": c["voice"], "model": c["model"],
             "renditions": rends.get(c["idx"], [])}
            for c in ch]}

    def text(self, q, body):
        """The chapter's text, for the now-playing page. Not the audio path."""
        try:
            idx = int(q["idx"])
            t = library.chapter_text(self.conn, self.storage, q["book"], idx)
        except (KeyError, ValueError):
            return 404, {"error": "no chapter"}
        rows = [c for c in library.chapters(self.conn, q["book"]) if c["idx"] == idx]
        # Reading blocks: the same text cut into sentences, each weighted by how
        # long the voice takes to say it, so the player can mark the one it is
        # speaking. Weights use the pronounced text, because that is what was
        # recorded. A client that does not know this field renders `text`.
        uid = self._uid(q, body)
        rules = pron.load(self.conn, uid) if uid else []
        blocks = reading.sentences(t, weigh=lambda s: len(pron.spoken(s, rules)))
        return 200, {"text": t, "title": rows[0]["title"] if rows else "",
                     "sentences": blocks}

    def chapter(self, q, body):
        idx = int(q["idx"])
        book_id = q["book"]
        voice = q.get("voice") or ""
        provider = q.get("provider") or ""
        model = q.get("model") or ""
        speed = q.get("speed") or ""
        if voice or provider:
            # a named voice: serve that rendition, and say so if it is not there
            r = library.rendition(self.conn, book_id, idx, provider, voice, model,
                                  speed=speed)
            if not r:
                return 409, {"error": "not rendered in that voice"}
            data = self.storage.get(r["audio_ref"])
            return 200, {"_raw": data, "_mime": "audio/mpeg", "bytes": len(data),
                         "checksum": r["audio_ref"]}
        rows = [r for r in library.chapters(self.conn, book_id) if r["idx"] == idx]
        if not rows:
            return 404, {"error": "no chapter"}
        r = rows[0]
        if not r["audio_ref"]:
            return 409, {"error": "not rendered"}
        data = self.storage.get(r["audio_ref"])
        return 200, {"_raw": data, "_mime": "audio/mpeg", "bytes": len(data),
                     "checksum": r["audio_ref"]}
