"""Sign-in providers, grants, and the admin screens (credit, plans, payments, roster).

A mixin of host.app.App: its methods are the handlers the route tables name,
moved here verbatim so no one file holds the whole server. They use the App's
own state (self.conn, self.host, ...) and each other through self.
"""

import re
from host import accounting, audit, auth, billing, connections, grants, payments, quota, settings, stats


class AdminRoutes:
    # ---- auth (social sign-in; the session lives in sqlite) ------------------

    def auth_providers(self, q, body):
        return 200, {"providers": self.auth.providers()}

    def auth_policy(self, q, body):
        """What a stranger may do before they have a session."""
        return 200, {
            "providers": self.auth.providers(),
            "has_accounts": auth.account_count(self.conn) > 0,
            "registration_open": auth.registration_open(self.conn),
        }

    def auth_users(self, q, body):
        uid = (q or {}).get("_user") or ""
        if not auth.is_admin(self.conn, uid):
            return 403, {"error": "admin only"}
        return 200, {"users": auth.local_accounts(self.conn),
                     "registration_open": auth.registration_open(self.conn),
                     "grants": grants.list_all(self.conn),
                     "you": uid}

    def grants_list(self, q, body):
        """Admin: every account and its lifted-limit grants."""
        uid = (q or {}).get("_user") or ""
        if not auth.is_admin(self.conn, uid):
            return 403, {"error": "admin only"}
        return 200, {"users": auth.local_accounts(self.conn),
                     "grants": grants.list_all(self.conn)}

    def grants_set(self, q, body):
        """Admin: lift or restore the storage and/or credit check for a user.
        The account's own usage and charges are untouched."""
        uid = (q or {}).get("_user") or ""
        if not auth.is_admin(self.conn, uid):
            return 403, {"error": "admin only"}
        target = (body or {}).get("user") or ""
        if not target:
            return 400, {"error": "user required"}
        body = body or {}
        out = grants.set_grants(
            self.conn, target,
            storage=(bool(body.get("storage_unlimited"))
                     if "storage_unlimited" in body else None),
            credits=(bool(body.get("credits_unlimited"))
                     if "credits_unlimited" in body else None))
        return 200, {"ok": True, "user": target, "grants": out}

    # ---- balance adjustments (admin) ----------------------------------------
    # Support and comps: a specific amount of credit on one account, added or
    # taken back, with a name on the row. A plan comp and a limit grant already
    # exist; this is the missing third thing, because neither of those puts a
    # dollar on a balance and the screen had no route that did.

    def admin_credit(self, q, body):
        """Admin: add or remove credit on one account.

        `amount` is signed: +5 adds five dollars, -5 takes five back. Every
        attempt writes one audit line, applied or refused, so a balance that did
        not move still has evidence that someone tried. Both balances come back
        on success, so the caller sees the number it produced.
        """
        uid = self._admin_only(q)
        if not uid:
            return 403, {"error": "admin only"}
        body = body or {}
        target = (body.get("user") or "").strip()
        who = self._account_name(uid)
        ip = (q or {}).get("_ip") or ""

        def refused(code, err, amount=0.0):
            audit.record(self.conn, uid, who, "adjust", target, amount,
                         outcome="refused", reason=err, ip=ip)
            return code, {"error": err}

        if not target:
            return refused(400, "user required")
        row = self.conn.execute("SELECT id FROM users WHERE id=?", (target,)).fetchone()
        if not row:
            return refused(404, "no such user")
        try:
            amount = float(body.get("amount"))
        except (TypeError, ValueError):
            return refused(400, "amount must be a number")
        if amount == 0:
            return refused(400, "amount must not be zero")
        # A fat-fingered 500 instead of 5 is the one mistake this screen invites,
        # so one entry is capped. Two entries are allowed and say so.
        if abs(amount) > 1000:
            return refused(400, "one entry may be at most 1000 "
                                "(do it in two entries if it is real)", amount)
        amount = round(amount, 2)
        reason = re.sub(r"\s+", " ", (body.get("note") or "")).strip()[:200]
        note = "%s %s%s" % (("credit by %s" % who) if amount > 0
                            else ("debit by %s" % who),
                            ("%.2f" % abs(amount)),
                            (": " + reason) if reason else "")
        before = accounting.balance(self.conn, target)
        try:
            after = accounting.adjust(self.conn, target, amount, note=note)
        except accounting.BelowZero as e:
            return refused(400, "would take the balance below zero", amount)
        audit.record(self.conn, uid, who, "adjust", target, amount,
                     before=before, after=after, outcome="applied",
                     note=reason, ip=ip)
        return 200, {"ok": True, "user": target, "amount": amount,
                     "before": round(before, 2), "balance": round(after, 2),
                     "currency": accounting.currency(self.conn, target) or "USD",
                     "note": note}

    def admin_audit(self, q, body):
        """Admin: the staff action log, newest first."""
        if not self._admin_only(q):
            return 403, {"error": "admin only"}
        limit = min(int((q or {}).get("limit") or 20), audit.MAX)
        return 200, {"entries": audit.recent(self.conn, limit)}

    # ---- plans (admin) -------------------------------------------------------
    # An admin sets what tier an account is on without a payment. This is how a
    # comped account (family, a tester) gets the paid tier; usage and the
    # allowance are the tier's own, so the screen stays honest.

    def plans_admin(self, q, body):
        """Admin: every account, its plan, and its storage blocks."""
        uid = (q or {}).get("_user") or ""
        if not auth.is_admin(self.conn, uid):
            return 403, {"error": "admin only"}
        subs = billing.list_all(self.conn)
        admins = {r["user_id"] for r in
                  self.conn.execute("SELECT user_id FROM admins")}
        rows = []
        for r in self.conn.execute("SELECT id,name FROM users ORDER BY created_at"):
            uid_row = r["id"]
            sub = subs.get(uid_row)
            rows.append({
                "username": r["name"] or uid_row,
                "user_id": uid_row,
                "admin": uid_row in admins,
                "plan": (sub or {}).get("plan") or "",
                "plan_status": (sub or {}).get("status") or "",
                "plan_provider": (sub or {}).get("provider") or "",
                "storage_blocks": billing.blocks(self.conn, uid_row),
            })
        return 200, {"users": rows,
                     "tiers": list(quota.TIERS)}

    def plans_admin_set(self, q, body):
        """Admin: set one account's tier (free or platform). Records it like a
        provider would, with `admin` as the provider, so nothing downstream can
        tell a comp from a paid row except the provider field."""
        uid = (q or {}).get("_user") or ""
        if not auth.is_admin(self.conn, uid):
            return 403, {"error": "admin only"}
        body = body or {}
        target = (body.get("user") or "").strip()
        plan = (body.get("plan") or "").strip().lower()
        tiers = list(quota.TIERS)
        if not target:
            return 400, {"error": "user required"}
        if plan not in tiers:
            return 400, {"error": "plan must be one of %s" % (tiers,)}
        row = billing.upsert(self.conn, target, plan, "active", provider="admin")
        return 200, {"ok": True, "subscription": row}

    # ---- payments setup (admin) ---------------------------------------------
    # Going live with the card provider used to be a paste-the-secrets-into-Azure
    # job, which meant the values were invisible, unvalidated, and could only
    # change with a new image. These three routes make it a screen: read what is
    # set (never the secrets themselves), save a batch, and ask the provider
    # whether the saved values are actually real.

    def _admin_only(self, q):
        uid = (q or {}).get("_user") or ""
        return uid if auth.is_admin(self.conn, uid) else ""

    def admin_billing(self, q, body):
        """Admin: the provider configuration, masked, and what still blocks a
        sale. A secret comes back as set:true plus its last four characters."""
        if not self._admin_only(q):
            return 403, {"error": "admin only"}
        out = settings.view(self.conn)
        out["provider"] = getattr(payments.get(), "id", "")
        out["payments_configured"] = payments.configured()
        return 200, out

    def admin_billing_save(self, q, body):
        """Admin: save a batch of settings and put them to work immediately.

        Validated as a batch before anything is written, so a refused save (a
        live key against the sandbox environment, two tiers sharing one price)
        leaves the working configuration alone. Applied and re-registered in
        the same breath: a key that is saved but not loaded is the shape of bug
        that has cost this project a live render more than once.
        """
        if not self._admin_only(q):
            return 403, {"error": "admin only"}
        body = body or {}
        values = body.get("values")
        if values is None:
            values = {k: v for k, v in body.items() if k in settings.BY_KEY}
        if not isinstance(values, dict):
            return 400, {"error": "values must be an object"}
        clear = [k for k in (body.get("clear") or []) if isinstance(k, str)]
        saved, errors = settings.save(self.conn, values, clear=clear)
        if errors:
            return 400, {"error": "not saved", "fields": errors,
                         "settings": settings.view(self.conn)}
        settings.apply(self.conn)
        payments.reload()
        out = settings.view(self.conn)
        out.update({"ok": True, "saved": saved,
                    "payments_configured": payments.configured(),
                    "provider": getattr(payments.get(), "id", "")})
        return 200, out

    def admin_billing_check(self, q, body):
        """Admin: ask Paddle whether the saved key and price ids exist.

        A saved value is a claim. This is the receipt: the key authenticates,
        the catalog holds every id we sell, and the webhook secret is stated as
        unproven, because only a real delivery can prove it.
        """
        if not self._admin_only(q):
            return 403, {"error": "admin only"}
        return 200, settings.check(self.conn)

    def admin_accounts(self, q, body):
        """Admin: one page of accounts as rows, with the numbers that decide what
        to do about each one.

        The admin screen used to stack three separate lists (grants, plans,
        sessions) that each repeated every account, then it became one long page
        of accordions. Both showed everything at once, so a server with fifty
        accounts was unreadable and every row paid for storage and stats whether
        or not anyone looked at it. This is a control: `offset`/`limit` pick the
        page, `filter` narrows the roster, and the expensive numbers are only
        measured for the rows actually returned. `total` is what the pager needs
        and is computed before any of that.

        Everything here is measured, per account: what the shelf holds, what has
        been voiced, how long it has been listened to, when it was last seen.
        A missing row is zero, never a guess.
        """
        uid = (q or {}).get("_user") or ""
        if not auth.is_admin(self.conn, uid):
            return 403, {"error": "admin only"}
        q = q or {}
        try:
            limit = int(q.get("limit") or 10)
        except (TypeError, ValueError):
            limit = 10
        try:
            offset = int(q.get("offset") or 0)
        except (TypeError, ValueError):
            offset = 0
        limit = max(1, min(limit, 100))
        offset = max(0, offset)
        want = (q.get("filter") or "").strip().lower()

        admins = {r["user_id"] for r in
                  self.conn.execute("SELECT user_id FROM admins")}
        named = {}
        for r in self.conn.execute("SELECT username,user_id FROM local_accounts"):
            named.setdefault(r["user_id"], r["username"])
        # The roster first: cheap columns only, so the pager can be answered
        # without paying for storage reads on accounts nobody is looking at.
        base = []
        for r in self.conn.execute("SELECT id,name,created_at FROM users"):
            who = r["id"]
            base.append({"user_id": who,
                         "username": named.get(who) or r["name"] or who,
                         "admin": who in admins,
                         "created_at": r["created_at"]})
        if want:
            base = [b for b in base
                    if want in (b["username"] or "").lower()
                    or want in (b["user_id"] or "").lower()]
        base.sort(key=lambda b: (not b["admin"], (b["username"] or "").lower()))
        total = len(base)
        page = base[offset:offset + limit]
        if not page:
            return 200, {"accounts": [], "you": uid, "total": total,
                         "offset": offset, "limit": limit, "filter": want,
                         "tiers": list(quota.TIERS),
                         "registration_open": auth.registration_open(self.conn)}

        subs = billing.list_all(self.conn)
        all_grants = grants.list_all(self.conn)
        books = {}
        for r in self.conn.execute(
                "SELECT user_id,COUNT(*) n FROM books GROUP BY user_id"):
            books[r["user_id"]] = int(r["n"] or 0)
        seen, sessions = {}, {}
        for s in auth.list_all_sessions(self.conn, q.get("_token") or ""):
            u = s["user"]
            sessions[u] = sessions.get(u, 0) + 1
            seen[u] = max(seen.get(u, 0), int(s.get("last_seen") or 0))
        rows = []
        for b in page:
            who = b["user_id"]
            sub = subs.get(who) or {}
            plan = sub.get("plan") or quota.FREE_PLAN
            blocks = billing.blocks(self.conn, who)
            g = all_grants.get(who) or {}
            use = stats.user_stats(self.conn, who)
            try:
                stor = quota.usage(self.conn, self.storage, who)["own_bytes"]
            except Exception:
                stor = 0
            rows.append({
                "user_id": who,
                "username": b["username"],
                "admin": b["admin"],
                "created_at": int(b["created_at"] or 0) if str(b["created_at"] or "").isdigit() else 0,
                "plan": plan,
                "plan_status": sub.get("status") or "",
                "plan_provider": sub.get("provider") or "",
                "storage_blocks": blocks,
                "storage_bytes": stor,
                "allowance_bytes": quota.allowance_bytes(plan, blocks),
                "storage_unlimited": bool(g.get("storage_unlimited")),
                "credits_unlimited": bool(g.get("credits_unlimited")),
                "balance": round(accounting.balance(self.conn, who), 2),
                "currency": accounting.currency(self.conn, who) or "USD",
                "books": books.get(who, 0),
                "chars_voiced": int(use.get("chars_total") or 0),
                "books_listened": int(use.get("books_listened") or 0),
                "listen_seconds": float(use.get("listen_seconds") or 0.0),
                "sessions": sessions.get(who, 0),
                "last_seen": seen.get(who, 0),
            })
        return 200, {"accounts": rows, "you": uid, "total": total,
                     "offset": offset, "limit": limit, "filter": want,
                     "tiers": list(quota.TIERS),
                     "registration_open": auth.registration_open(self.conn)}

    def set_registration(self, q, body):
        uid = (q or {}).get("_user") or ""
        if not auth.is_admin(self.conn, uid):
            return 403, {"error": "admin only"}
        v = auth.set_registration(self.conn, bool((body or {}).get("open")))
        return 200, {"ok": True, "registration": v}

    def me(self, q, body):
        uid = (q or {}).get("_user") or ""
        return 200, {"user": uid, "authenticated": bool(uid),
                     "admin": auth.is_admin(self.conn, uid),
                     # about yourself only: the account panel reads this to ask
                     # for a password (local) or a typed phrase (social-only).
                     "has_password": bool(uid) and auth.has_local_account(self.conn, uid),
                     # the account's theme: a new browser asks here, because the
                     # browser's own copy says nothing about a machine it has
                     # never seen.
                     "theme": connections.get_theme(self.conn, uid) if uid else ""}

    def auth_linked(self, q, body):
        """Which social providers the signed-in user has attached."""
        uid = (q or {}).get("_user") or ""
        if not uid:
            return 401, {"error": "auth required"}
        return 200, {"linked": auth.linked_providers(self.conn, uid)}
