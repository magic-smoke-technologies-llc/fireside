"""The signed-in account: second factor, sessions, password, deletion.

A mixin of host.app.App: its methods are the handlers the route tables name,
moved here verbatim so no one file holds the whole server. They use the App's
own state (self.conn, self.host, ...) and each other through self.
"""

from host import auth, billing, db, erase, payments
from host.appsupport import DELETE_PHRASES, _norm_confirm, _truthy


class AccountRoutes:
    # ---- the second factor ---------------------------------------------------
    # Six login paths mint sessions, and none of them mentions two-factor: the
    # gate lives in the dispatch table plus auth.create_session. These handlers
    # are the only places that know a factor exists.

    def _twofa_who(self, q):
        """(factor, user_id) for the caller, pending or not.

        A half-session has a user id but no authority, so every handler below
        reads both from here instead of from q["_user"], which is "" until the
        factor is done.
        """
        factor, uid = auth.session_state(self.conn, (q or {}).get("_token") or "")
        return (factor or ""), (uid or "")

    def _account_name(self, uid):
        """What the authenticator app should label this account as."""
        row = self.conn.execute("SELECT username FROM local_accounts WHERE user_id=?",
                               (uid,)).fetchone()
        return ((row["username"] if row else "") or uid)

    def _allow_attempt(self, uid, kind):
        return self.door_limiter.allow("2fa:%s:%s" % (kind, uid or "?"))

    def twofa_status(self, q, body):
        """What this session still owes, and what it may do about it.

        `required` means this account must carry a factor: an administrator
        always, everyone else when the deployment requires it. The panel says
        what is true rather than what is allowed, so "can_disable" is false
        exactly when a factor is not the account's own choice."""
        factor, uid = self._twofa_who(q)
        admin = bool(uid) and auth.is_admin(self.conn, uid)
        forced = auth.must_have_factor(self.conn, uid)
        # an admin who is the only account on the box is not held at an
        # enrolment wall (first run, own front door); the rule is deferred, not
        # dropped, and it applies again the moment a second account exists.
        deferred = forced and auth.bootstrap_skip(self.conn, uid)
        return 200, {
            "twofa": factor if factor in auth.PENDING else "",
            "enrolled": auth.enrolled(self.conn, uid),
            "recovery_left": auth.recovery_code_left(self.conn, uid),
            "required": forced,
            "deferred": bool(deferred) and not auth.enrolled(self.conn, uid),
            "admin": admin,
            "can_disable": bool(uid) and not forced,
        }

    def twofa_enroll(self, q, body):
        """Start enrolment: a fresh secret and the otpauth:// URI for the phone.

        Starting over is safe and deliberate: begin_enroll replaces the secret
        and un-confirms it, so an interrupted enrolment cannot leave a key
        behind that nobody scanned. That is exactly why replacing a CONFIRMED
        factor asks for the current one first. Without that proof a stolen
        session could move the second factor onto its own phone, which is the
        whole thing the factor exists to stop.
        """
        factor, uid = self._twofa_who(q)
        if not uid:
            return 401, {"error": "auth required"}
        if not self._allow_attempt(uid, "enroll"):
            return 429, {"error": "too many attempts"}
        b = body or {}
        changing = auth.enrolled(self.conn, uid)
        if changing:
            ok = auth.check_code(self.conn, uid, b.get("code"))
            if not ok and (b.get("recovery") or "").strip():
                ok = auth.spend_recovery_code(self.conn, uid, b.get("recovery"))
            if not ok:
                return 401, {"error": "enter a current code to change your "
                                      "authenticator", "needs_proof": True}
        account = b.get("account") or self._account_name(uid)
        out = auth.begin_enroll(self.conn, uid, account)
        out["changing"] = changing
        out["twofa"] = factor if factor in auth.PENDING else ""
        return 200, out

    def twofa_confirm(self, q, body):
        """Prove the secret works. Nothing may depend on it before this passes."""
        factor, uid = self._twofa_who(q)
        if not uid:
            return 401, {"error": "auth required"}
        if not self._allow_attempt(uid, "confirm"):
            return 429, {"error": "too many attempts"}
        codes = auth.confirm_enroll(self.conn, uid, (body or {}).get("code"))
        if codes is None:
            return 400, {"error": "that code did not match"}
        out = {"ok": True, "twofa": "", "recovery": codes,
               "recovery_left": len(codes)}
        if factor in auth.PENDING:
            # an admin finishing a forced enrolment is not made to log in a
            # second time: the same session becomes a full one here.
            auth.satisfy_factor(self.conn, (q or {}).get("_token") or "")
        return 200, out

    def _code_refusal(self, uid, code):
        """A refusal to return for a typed code, or None when it was good.

        "already been used" is its own sentence because the commonest replay is
        innocent: right after turning the factor on, the same digits are still
        on the phone. Saying "did not match" to that sends a person round the
        same loop with a code that was right.
        """
        st = auth.check_code_status(self.conn, uid, code)
        if st == "ok":
            return None
        if st == "replayed":
            return 401, {"error": "that code has already been used; wait for the "
                                  "next one, then type it"}
        return 401, {"error": "that code did not match"}

    def twofa_verify(self, q, body):
        """Finish a login: a TOTP code or a recovery code, then a full session."""
        factor, uid = self._twofa_who(q)
        if not uid:
            return 401, {"error": "auth required"}
        if factor not in auth.PENDING:
            return 200, {"ok": True, "twofa": ""}
        if not self._allow_attempt(uid, "verify"):
            return 429, {"error": "too many attempts"}
        b = body or {}
        used = "code"
        if (b.get("recovery") or "").strip():
            used = "recovery"
            if not auth.spend_recovery_code(self.conn, uid, b.get("recovery")):
                return 401, {"error": "that recovery code is not valid"}
        else:
            bad = self._code_refusal(uid, b.get("code"))
            if bad:
                return bad
        auth.satisfy_factor(self.conn, (q or {}).get("_token") or "")
        return 200, {"ok": True, "user": uid, "twofa": "", "used": used,
                     "recovery_left": auth.recovery_code_left(self.conn, uid)}

    def twofa_recovery(self, q, body):
        """New set of one-time codes, for an already enrolled account."""
        factor, uid = self._twofa_who(q)
        if not uid or factor in auth.PENDING or not auth.enrolled(self.conn, uid):
            return 401, {"error": "enrol two-factor first"}
        if not self._allow_attempt(uid, "recovery"):
            return 429, {"error": "too many attempts"}
        bad = self._code_refusal(uid, (body or {}).get("code"))
        if bad:
            return bad
        codes = auth.issue_recovery_codes(self.conn, uid)
        return 200, {"ok": True, "recovery": codes, "recovery_left": len(codes)}

    def twofa_disable(self, q, body):
        """Turn the factor off, given a current code.

        An administrator is always refused: an admin account carries a factor,
        full stop, and this is the route that would quietly undo that. Refusing
        is not a lockout, because there are three ways back in that keep the
        factor on: a recovery code finishes a login, changing the authenticator
        accepts a recovery code as proof, and tools/reset_2fa.py clears the
        factor from inside the container, after which the next login must enrol
        again. A non-admin is refused only when the deployment requires a factor
        of everyone.
        """
        factor, uid = self._twofa_who(q)
        if not uid or factor in auth.PENDING:
            return 401, {"error": "auth required"}
        if auth.must_have_factor(self.conn, uid):
            why = ("administrators must keep a second factor"
                   if auth.is_admin(self.conn, uid)
                   else "this server requires a second factor for every account")
            return 403, {"error": why + "; you can change it, or use a recovery "
                                       "code"}
        if not self._allow_attempt(uid, "disable"):
            return 429, {"error": "too many attempts"}
        b = body or {}
        bad = self._code_refusal(uid, b.get("code"))
        if bad and (b.get("recovery") or "").strip():
            if auth.spend_recovery_code(self.conn, uid, b.get("recovery")):
                bad = None
        if bad:
            return bad
        auth.disable_2fa(self.conn, uid)
        return 200, {"ok": True, "enrolled": False}

    # ---- devices and sessions (see and end a session anywhere) ---------------

    def _session_state(self, q, uid):
        """The caller's own sessions, plus (for an admin) every session on the
        server, so the same panel can boot a device anywhere."""
        tok = (q or {}).get("_token") or ""
        out = {"sessions": auth.list_sessions(self.conn, uid, tok)}
        if auth.is_admin(self.conn, uid):
            out["admin"] = True
            out["all"] = auth.list_all_sessions(self.conn, tok)
        return out

    def sessions_list(self, q, body):
        """Every live session of the signed-in user, the current one flagged.
        An admin also gets every session on the server."""
        uid = (q or {}).get("_user") or ""
        if not uid:
            return 401, {"error": "auth required"}
        return 200, self._session_state(q, uid)

    def session_revoke(self, q, body):
        """Log out one session by its id. A user may end any of their own; an
        admin may end any session on the server. A stale id is not found."""
        uid = (q or {}).get("_user") or ""
        if not uid:
            return 401, {"error": "auth required"}
        sid = ((body or {}).get("id") or "").strip()
        ok = auth.revoke_session(self.conn, uid, sid)
        if not ok and auth.is_admin(self.conn, uid):
            ok = auth.revoke_any(self.conn, sid) is not None
        out = self._session_state(q, uid)
        out["ok"] = ok
        return (200 if ok else 404), out

    def sessions_revoke_all(self, q, body):
        """Log out: the caller's own sessions (default), or, for an admin with
        {all_users: true}, every session on the server. Either way keep the one
        calling, so the button does not sign you out of the page you are on."""
        uid = (q or {}).get("_user") or ""
        if not uid:
            return 401, {"error": "auth required"}
        others = (body or {}).get("others")
        keep_current = others is None or _truthy(others)
        keep = ((q or {}).get("_token") or "") if keep_current else ""
        if _truthy((body or {}).get("all_users")):
            if not auth.is_admin(self.conn, uid):
                return 403, {"error": "admin only"}
            n = auth.revoke_all_sessions(self.conn, keep)
        else:
            n = auth.revoke_sessions(self.conn, uid, keep)
        out = self._session_state(q, uid)
        out["ok"] = True
        out["revoked"] = n
        return 200, out

    # ---- erasing the account --------------------------------------------------

    # ---- the account's own password --------------------------------------------

    def account_password(self, q, body):
        """Set or change the signed-in account's password.

        A password account must give the current one. A social-only account has
        none, so it names itself a sign-in name and gets a password. Either way
        every OTHER session is revoked: a password change should end sessions
        won under the old one, while the device doing the changing stays in.
        """
        uid = (q or {}).get("_user") or ""
        if not uid:
            return 401, {"error": "auth required"}
        body = body or {}
        try:
            name = auth.set_password(self.conn, uid,
                                     body.get("current") or "",
                                     body.get("password") or "",
                                     body.get("username") or "")
        except auth.AuthError as e:
            return 400, {"error": str(e)}
        ended = auth.revoke_sessions(self.conn, uid, (q or {}).get("_token") or "")
        return 200, {"ok": True, "username": name, "has_password": True,
                     "ended": ended}

    def _cancel_billing_for(self, uid):
        """Stop the recurring charge before the account is erased.

        Deleting the rows on our side never reached the provider, so a deleted
        account kept being invoiced. Asking first is the whole fix; a failure is
        reported rather than hidden, because an invoice arriving for a customer
        who no longer exists is exactly the kind of thing the buyer must not
        have to discover alone.
        """
        sub = billing.get(self.conn, uid) or {}
        sid = sub.get("subscription_id") or ""
        if not sid:
            return {"cancelled": False, "detail": "no subscription on file"}
        pp = payments.get(sub.get("provider") or None)
        fn = getattr(pp, "cancel", None) if pp is not None else None
        if not callable(fn):
            return {"cancelled": False, "detail": "payments not configured"}
        res = fn(sid)
        return {"cancelled": bool(res.get("ok")),
                "subscription_id": sid,
                "detail": res.get("detail", "")}

    def account_delete(self, q, body):
        """Erase the signed-in account for good: books, voiced audio, history,
        credentials, and sign-ins.

        Proof first, the way the account already signs in: a password account
        proves with its password; a social-only account has no password to give,
        so it must type the confirmation phrase. The last admin cannot delete
        itself - that would leave the server with nobody who can run it."""
        uid = (q or {}).get("_user") or ""
        if not uid:
            return 401, {"error": "auth required"}
        body = body or {}
        if auth.is_admin(self.conn, uid) and auth.admin_count(self.conn) <= 1:
            return 409, {"error": "last admin",
                         "detail": "this is the only admin account. Make another "
                                   "account an admin and delete this one after."}
        if auth.has_local_account(self.conn, uid):
            if not auth.local_password_proof(self.conn, uid,
                                             body.get("password") or ""):
                return 403, {"error": "wrong password"}
        elif _norm_confirm(body.get("confirm")) not in DELETE_PHRASES:
            return 400, {"error": "confirm",
                         "detail": "type 'delete my account' to confirm"}
        billing_out = self._cancel_billing_for(uid)
        out = erase.purge_user(self.conn, self.storage, uid)
        if billing_out.get("subscription_id") and not billing_out["cancelled"]:
            # keep a trail an operator can finish by hand, since the rows that
            # named the subscription are gone with the account
            self._claim_payment("cancel-failed:%s:%s" % (uid, db.now()),
                                "cancel_failed", uid,
                                billing_out.get("subscription_id", ""), 0,
                                billing_out.get("detail", ""))
        out["billing"] = billing_out
        out["ok"] = True
        out["logout"] = True
        return 200, out

    def admin_account_delete(self, q, body):
        """Admin: erase somebody else's account.

        Approval to delete is not the same as taking the decision to delete, so
        the confirmation is the target's own name typed out, not a button. This
        never deletes the calling admin (the account page is the place for
        that, and it knows the password rules) and never deletes another admin:
        an admin removes admin rights where it leaves a trace, rather than by
        erasing the person.
        """
        uid = (q or {}).get("_user") or ""
        if not auth.is_admin(self.conn, uid):
            return 403, {"error": "admin only"}
        body = body or {}
        target = (body.get("user") or "").strip()
        if not target:
            return 400, {"error": "user required"}
        if target == uid:
            return 409, {"error": "that is your own account",
                         "detail": "delete your own account from Settings > Account"}
        if auth.is_admin(self.conn, target):
            return 409, {"error": "that account is an admin",
                         "detail": "remove the admin row first, then delete it"}
        row = self.conn.execute("SELECT id,name FROM users WHERE id=?",
                                (target,)).fetchone()
        if not row:
            return 404, {"error": "no such account"}
        typed = (body.get("confirm") or "").strip()
        expect = {target}
        for r in self.conn.execute("SELECT username FROM local_accounts WHERE user_id=?",
                                   (target,)):
            expect.add(r["username"])
        if row["name"]:
            expect.add(row["name"])
        if typed not in expect:
            return 400, {"error": "confirm",
                         "detail": "type the account's username to confirm"}
        billing_out = self._cancel_billing_for(target)
        out = erase.purge_user(self.conn, self.storage, target)
        auth.revoke_sessions(self.conn, target, "")
        if billing_out.get("subscription_id") and not billing_out["cancelled"]:
            self._claim_payment(
                "cancel-failed:%s:%s" % (target, db.now()), "cancel_failed",
                target, billing_out.get("subscription_id", ""), 0,
                billing_out.get("detail", ""))
        return 200, {"ok": True, "user": target, "books": out.get("books", 0),
                     "freed": out.get("freed", 0), "billing": billing_out}
