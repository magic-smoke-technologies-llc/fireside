"""Listening and voices: the sandbox, playback, credentials, connections, pronunciation.

A mixin of host.app.App: its methods are the handlers the route tables name,
moved here verbatim so no one file holds the whole server. They use the App's
own state (self.conn, self.host, ...) and each other through self.
"""

from host import auth, connections, credentials, playback, pron, stats


class VoiceRoutes:
    # ---- voice sandbox --------------------------------------------------------
    # Say a short bit of free text in one voice, right now, and hand the audio
    # straight back. Not a render: nothing is stored, nothing is metered, and the
    # only limit is the provider's own. The sandbox page drives this once per
    # voice for the scan button.

    SPEAK_MAX_CHARS = 1000

    def speak(self, q, body):
        uid = self._uid(q, body)
        text = (body.get("text") or "").strip()
        if not text:
            return 400, {"error": "nothing to say"}
        if len(text) > self.SPEAK_MAX_CHARS:
            return 400, {"error": "text is longer than %d characters" % self.SPEAK_MAX_CHARS}
        cid = body.get("connection_id") or ""
        provider = body.get("provider", "")
        cred = None
        if cid:
            c = connections.get(self.conn, cid)
            if not connections.may_use(c, uid, auth.is_admin(self.conn, uid)):
                return 403, {"error": "no such connection"}
            cred = connections.resolve(self.conn, uid, cid)
            if cred is None:
                return 403, {"error": "connection is switched off"}
            provider = c["provider"]
        if provider not in self.host.speech:
            return 400, {"error": "unknown provider"}
        if cred is None:
            cred = body.get("cred") or credentials.get_credential(self.conn, uid, provider)
        voice = body.get("voice", "")
        model = body.get("model", "")
        speed = float(body.get("speed", 1.0) or 1.0)
        if self.rate_limiter is not None and not self.rate_limiter.allow(uid):
            return 429, {"error": "rate limited"}
        speech = self.host.speech[provider].obj
        try:
            audio = speech.synth(text, voice, {"model": model, "speed": speed}, cred)
        except Exception as e:
            # The caller's own voice server (a wrong key, a dead host) is theirs
            # to read about; it is the sandbox's whole point. Not a server fault.
            return 502, {"error": "the voice server failed", "detail": str(e)[:300]}
        data = getattr(audio, "data", None) or getattr(audio, "bytes", None) or b""
        if not data:
            return 502, {"error": "the voice server returned no audio"}
        return 200, {"_raw": data, "_mime": getattr(audio, "mime", "audio/mpeg"),
                     "bytes": len(data), "chars": len(text)}

    # ---- playback -------------------------------------------------------------

    def get_position(self, q, body):
        return 200, playback.get_position(self.conn, self._uid(q, None), q["book"]) or {}

    def last_position(self, q, body):
        return 200, playback.last_position(self.conn, self._uid(q, None))

    def put_position(self, q, body):
        return 200, playback.put_position(self.conn, self._uid(q, body), body["book"],
                                          body["chapter_index"], body["offset_seconds"],
                                          body.get("device", ""))

    def claim(self, q, body):
        return 200, playback.claim(self.conn, self._uid(q, body), body["book"], body["device"])

    def stop(self, q, body):
        return 200, {"stop": playback.stop_pending(self.conn, self._uid(q, None), q["book"],
                                                   q["device"])}

    def ack(self, q, body):
        playback.ack_stop(self.conn, self._uid(q, body), body["book"], body["device"])
        return 200, {"ok": True}

    def stats_get(self, q, body):
        return 200, stats.user_stats(self.conn, self._uid(q, None))

    def listen(self, q, body):
        stats.add_listen(self.conn, self._uid(q, body), body.get("book", ""),
                         body.get("seconds", 0))
        return 200, {"ok": True}

    # ---- credentials (hosted; values never leave) -----------------------------

    def put_credential(self, q, body):
        credentials.set_credential(self.conn, self._uid(q, body), body["provider"],
                                   body.get("data") or {})
        return 200, {"ok": True, "providers": credentials.providers(self.conn, self._uid(q, body))}

    # ---- source credentials (server-side, never returned) ---------------------

    def _source_field_defs(self, sid):
        for p in self.host.readers:
            if p.id == sid:
                return self._fields(p.obj)
        return []

    def source_creds(self, q, body):
        """Every reader that declares settings, with its fields and whether the
        signed-in user has a stored value. A value is never returned."""
        uid = self._uid(q, body)
        out = []
        for p in self.host.readers:
            fields = self._fields(p.obj)
            if not fields:
                continue
            stored = credentials.get_credential(self.conn, uid, "source:" + p.id)
            fs = []
            for f in fields:
                name = f.get("name") or f.get("key") or ""
                kind = f.get("kind") or f.get("type") or "text"
                sensitive = self._sensitive(name, kind)
                fs.append({"name": name, "label": f.get("label") or name,
                           "kind": kind, "help": f.get("help") or "",
                           "placeholder": f.get("placeholder") or "",
                           "sensitive": sensitive,
                           "set": bool(stored.get(name)) if stored else False,
                           "value": ""})
            out.append({"id": p.id, "label": p.label, "fields": fs})
        return 200, {"sources": out}

    def source_cred_save(self, q, body):
        """Store a source's credential server-side for the signed-in user. An
        empty field leaves the stored value alone, so re-saving a form that does
        not re-type a secret never blanks it."""
        uid = self._uid(q, body)
        sid = body.get("id") or ""
        defs = {f.get("name") or f.get("key"): (f.get("kind") or f.get("type") or "text")
                for f in self._source_field_defs(sid)}
        if not defs:
            return 400, {"error": "unknown source"}
        cur = credentials.get_credential(self.conn, uid, "source:" + sid)
        for k, v in (body.get("data") or {}).items():
            if k in defs and v not in (None, ""):
                cur[k] = v
        credentials.set_credential(self.conn, uid, "source:" + sid, cur)
        return 200, {"ok": True}

    def source_cred_delete(self, q, body):
        uid = self._uid(q, body)
        credentials.delete_credential(self.conn, uid, "source:" + (body.get("id") or ""))
        return 200, {"ok": True}

    # ---- connections (voice servers, kept on the server) ---------------------

    def _field_defs(self, provider):
        p = self.host.speech.get(provider)
        return self._fields(p.obj) if p else []

    def _sensitive(self, name, kind):
        return kind == "password" or name in ("key", "token", "cookie", "password", "secret")

    def _conn_view(self, c, uid, is_admin):
        """A connection as the client may see it: never a secret value."""
        fields = []
        for f in self._field_defs(c["provider"]):
            name = f.get("name") or f.get("key") or ""
            kind = f.get("kind") or f.get("type") or "text"
            merged = dict(c["data"]); merged.update(c.get("override") or {})
            val = merged.get(name)
            sensitive = self._sensitive(name, kind)
            fields.append({"name": name, "label": f.get("label") or name, "kind": kind,
                           "sensitive": sensitive, "set": val not in (None, ""),
                           "shown": f.get("shown", True) is not False,
                           "value": "" if sensitive else (val or "")})
        return {"id": c["id"], "scope": c["scope"], "provider": c["provider"],
                "label": c["label"], "mine": c["mine"], "system": c["system"],
                "enabled": c["enabled"],
                "editable": connections.may_edit(c, uid, is_admin), "fields": fields}

    def connections_list(self, q, body):
        uid = self._uid(q, body)
        is_admin = auth.is_admin(self.conn, uid)
        out = [self._conn_view(c, uid, is_admin)
               for c in connections.list_for(self.conn, uid)]
        prefs = connections.get_prefs(self.conn, uid)
        return 200, {"connections": out, "admin": is_admin,
                     "default_connection_id": prefs["default_connection_id"],
                     "default_voice": prefs["default_voice"],
                     "reading": prefs["reading"]}

    def _writable_fields(self, provider, uid, is_admin):
        """The field-name -> kind map for a provider (who may write what is
        decided by the caller)."""
        return {f.get("name") or f.get("key"): (f.get("kind") or f.get("type") or "text")
                for f in self._field_defs(provider)}

    def connection_save(self, q, body):
        uid = self._uid(q, body)
        is_admin = auth.is_admin(self.conn, uid)
        cid = body.get("id") or None
        scope = body.get("scope") or connections.PERSONAL
        provider = body.get("provider") or ""
        if cid:
            cur = connections.get(self.conn, cid)
            if not connections.may_edit(cur, uid, is_admin):
                return 403, {"error": "not yours to edit"}
            provider, scope = provider or cur["provider"], cur["scope"]
            # an admin may share a server with the household, or stop sharing it
            if is_admin and body.get("scope") in (connections.SYSTEM, connections.PERSONAL):
                scope = body["scope"]
        elif scope == connections.SYSTEM and not is_admin:
            return 403, {"error": "only an admin adds a shared server"}
        if provider not in self.host.speech:
            return 400, {"error": "unknown provider"}
        defs = self._writable_fields(provider, uid, is_admin)
        data = {k: v for k, v in (body.get("data") or {}).items()
                if k in defs and (is_admin or not self._sensitive(k, defs[k]))}
        cid = connections.upsert(self.conn, uid, provider, body.get("label"),
                                 data, scope=scope, conn_id=cid)
        return 200, {"ok": True, "id": cid}

    def connection_delete(self, q, body):
        uid = self._uid(q, body)
        cur = connections.get(self.conn, body.get("id") or "")
        if not connections.may_edit(cur, uid, auth.is_admin(self.conn, uid)):
            return 403, {"error": "not yours to remove"}
        connections.delete(self.conn, cur["id"])
        return 200, {"ok": True}

    def connection_override(self, q, body):
        """A user's own switch and non-secret edits for one connection."""
        uid = self._uid(q, body)
        if not uid:
            return 401, {"error": "auth required"}
        cur = connections.get(self.conn, body.get("id") or "")
        if not connections.may_use(cur, uid, auth.is_admin(self.conn, uid)):
            return 403, {"error": "no such connection"}
        defs = self._writable_fields(cur["provider"], uid, False)
        data = {k: v for k, v in (body.get("data") or {}).items()
                if k in defs and not self._sensitive(k, defs[k])}
        connections.set_override(self.conn, uid, cur["id"],
                                 enabled=body.get("enabled"), data=data)
        return 200, {"ok": True}

    def prefs_set(self, q, body):
        uid = self._uid(q, body)
        if not uid:
            return 401, {"error": "auth required"}
        kw = {}
        if "default_connection_id" in body:
            kw["connection_id"] = body.get("default_connection_id") or ""
        if "default_voice" in body:
            kw["voice"] = body.get("default_voice") or ""
        if "reading" in body and isinstance(body.get("reading"), dict):
            kw["reading"] = body.get("reading")
        if "theme" in body:
            kw["theme"] = body.get("theme") or ""
        connections.set_prefs(self.conn, uid, **kw)
        return 200, {"ok": True, "theme": connections.get_theme(self.conn, uid)}

    # ---- pronunciation fixes (per user, applied before any engine) ----------

    def pron_get(self, q, body):
        uid = self._uid(q, body)
        if not uid:
            return 200, {"rules": [], "model": []}
        own = pron.mine(self.conn, uid)
        # The editor shows what the reader typed. A model pass writes its guesses
        # at the same scopes, and showing them in this list makes them look like
        # the reader's own words (and a Save would write them into one). They are
        # handed over separately, with the dictionary id so they can be dropped.
        guesses = [{"id": d["id"], "scope": d["scope"], "scope_key": d["scope_key"],
                    "count": len(d["rules"])}
                   for d in pron.dicts(self.conn, uid) if d["name"] == pron.MODEL_DICT_NAME]
        return 200, {"rules": pron.fold(own), "model": guesses}

    def pron_set(self, q, body):
        uid = self._uid(q, body)
        if not uid:
            return 401, {"error": "auth required"}
        pron.save(self.conn, uid, body.get("rules"))
        # same shape as GET: the editor's list is the reader's words only
        return 200, {"ok": True, "rules": pron.fold(pron.mine(self.conn, uid))}

    def pron_dicts(self, q, body):
        """Every dictionary this user has, with its scope and words."""
        uid = self._uid(q, body)
        if not uid:
            return 401, {"error": "auth required"}
        one = q.get("id") or ""
        if one:
            got = pron.export_dict(self.conn, uid, one)
            if not got:
                return 404, {"error": "no such dictionary"}
            return 200, got
        return 200, {"dicts": pron.dicts(self.conn, uid),
                     "scopes": list(pron.SCOPES),
                     "unknown": {"author": pron.UNKNOWN_AUTHOR,
                                 "series": pron.UNKNOWN_SERIES}}

    def pron_dict_save(self, q, body):
        """Create or update one dictionary, or import one from a file."""
        uid = self._uid(q, body)
        if not uid:
            return 401, {"error": "auth required"}
        if body.get("import"):
            got = pron.import_dict(self.conn, uid, body.get("dict") or body.get("rules"),
                                   body.get("scope"), body.get("scope_key"),
                                   body.get("name") or "")
            if not got:
                return 400, {"error": "not a dictionary file"}
            return 200, {"ok": True, "dict": got}
        got = pron.save_dict(self.conn, uid, body.get("name") or "",
                             body.get("scope") or "global",
                             body.get("scope_key") or "",
                             body.get("rules") or [],
                             body.get("id") or "")
        if not got:
            return 400, {"error": "a scope needs a name to attach to"}
        return 200, {"ok": True, "dict": got}

    def pron_dict_delete(self, q, body):
        uid = self._uid(q, body)
        if not uid:
            return 401, {"error": "auth required"}
        did = body.get("id") or q.get("id") or ""
        if not did:
            return 400, {"error": "which dictionary?"}
        pron.delete_dict(self.conn, uid, did)
        return 200, {"ok": True}

    def pron_impact(self, q, body):
        """Which already-voiced chapters a rule change makes out of date.

        Read-only: it names the work (per book, voice and chapter) so the UI can
        offer to re-encode just that, instead of a whole-book convert.
        """
        uid = self._uid(q, body)
        if not uid:
            return 401, {"error": "auth required"}
        return 200, pron.impact(self.conn, self.storage, uid,
                                q.get("book") or None,
                                split_for=self._speech_split)

    def _speech_split(self, provider):
        """A provider's chunk function, or None. Lets the impact report measure
        the truly new audio instead of assuming a whole chapter re-renders."""
        p = self.host.speech.get(provider)
        return getattr(p.obj, "split", None) if p else None
