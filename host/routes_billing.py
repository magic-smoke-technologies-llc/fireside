"""Credits, the recurring tier, checkout and the payment webhook.

A mixin of host.app.App: its methods are the handlers the route tables name,
moved here verbatim so no one file holds the whole server. They use the App's
own state (self.conn, self.host, ...) and each other through self.
"""

from host import accounting, allowance, billing, connections, grants, notices, paymentapply, payments, plans, quota
from host.appsupport import _ledger_row, _sale_offer, _with_idem


class BillingRoutes:
    # ---- credits (resold speech capacity, counted in characters) -------------
    # The sale connection's plugin is the authority on the rate and on how many
    # characters a payment buys; the host only holds the per-user character
    # balance and moves it. A metered run holds its estimate, the plugin reports
    # the real count, the host settles. Nothing here knows what a character costs.

    def _sale_connection(self, uid):
        """(provider, speech plugin, stored cred) for the designated sale
        connection. Pricing reads the connection's STORED data, never a user's
        override: a buyer must not be able to edit the rate they are charged."""
        spec = self.sale_connection or {}
        c = None
        cid = spec.get("connection_id")
        if cid:
            c = connections.get(self.conn, cid)
        elif spec.get("provider"):
            for cand in connections.list_for(self.conn, uid):
                if cand["provider"] == spec["provider"]:
                    c = cand
                    break
        if not c:
            return None, None, None
        p = self.host.speech.get(c["provider"])
        if p is None:
            return None, None, None
        return c["provider"], p.obj, dict(c["data"])

    def credits(self, q, body):
        """The signed-in user's character balance, plus what a sale connection
        would charge. Read-only: buying happens at /credits/purchase."""
        uid = self._uid(q, body)
        allowance.welcome(self.conn, uid)
        allowance.due(self.conn, uid)
        out = {
            "balance": accounting.balance(self.conn, uid),
            "reserved": accounting.reserved_total(self.conn, uid),
            "available": accounting.available(self.conn, uid),
            # Read the unit, do not assume it: during the switch to dollars an
            # old account still holds characters, and printing "chars" over a
            # dollar balance (or the reverse) is the one lie a wallet cannot
            # afford.
            "unit": accounting.currency(self.conn, uid) or "USD",
            "payments_configured": payments.configured(),
            "sale": None,
        }
        provider, speech, cred = self._sale_connection(uid)
        if speech is not None:
            out["sale"] = _sale_offer(provider, speech, cred)
        return 200, out

    def credits_history(self, q, body):
        """The last rows of this user's own ledger, newest first.

        The wallet is the one place where money changing is visible, so an
        absorbed overage has to be a row a person can read - estimate against
        final - rather than a line in a support log. Read-only."""
        uid = self._uid(q, body)
        if not uid:
            return 401, {"error": "auth required"}
        rows = accounting.ledger_entries(self.conn, uid)[-50:]
        rows.reverse()
        return 200, {
            "unit": accounting.currency(self.conn, uid) or "USD",
            "entries": [_ledger_row(r) for r in rows],
        }

    def credits_purchase(self, q, body):
        """Buy character capacity. Held until a payment provider is configured.

        The plugin sets how many characters a dollar amount buys (rounded down),
        and that number is pinned in the checkout metadata at the moment the
        buyer is quoted it - the server never re-derives the count later from a
        rate that may have moved. Card entry happens on the provider's hosted
        page; the host sees only a session id, and credits once that session
        reads back paid.
        """
        uid = self._uid(q, body)
        if not uid:
            return 401, {"error": "auth required"}
        if not self._limit(self.buy_limiter, uid):
            return 429, {"error": "rate limited"}
        idem = (q or {}).get("_idem", "")
        hit = self._idem_get(uid, idem)
        if hit:
            return hit
        provider, speech, cred = self._sale_connection(uid)
        if speech is None:
            return 400, {"error": "no sale connection configured"}
        try:
            usd = float((body or {}).get("usd") or 0)
        except (TypeError, ValueError):
            return 400, {"error": "bad amount"}
        if usd <= 0:
            return 400, {"error": "amount must be greater than zero"}
        pp = payments.get()
        if pp is None:
            return 503, {"error": "payments not configured",
                         "detail": "purchases are held until a payment provider lands"}
        to_chars = getattr(speech, "chars_for_payment", None)
        chars = int(to_chars(usd, cred)) if callable(to_chars) else 0
        if chars <= 0:
            return 400, {"error": "amount too small to buy characters"}
        hosted = getattr(pp, "purchase_checkout", None)
        if callable(hosted):
            co = _with_idem(hosted, usd,
                            {"user": uid, "provider": provider, "chars": chars,
                             "return_url": self._safe_return(
                                 (body or {}).get("return_url"),
                                 (q or {}).get("_base", ""))}, idem=idem)
            if not getattr(co, "ok", False):
                return 402, {"error": "checkout refused",
                             "detail": getattr(co, "detail", "")}
            payload = {"ok": True, "status": "awaiting_payment", "chars": chars,
                       "usd": round(usd, 2), "url": co.url,
                       "reference": co.reference,
                       "balance": accounting.balance(self.conn, uid)}
            self._idem_put(uid, idem, 200, payload)
            return 200, payload
        # A provider with no hosted checkout still has charge(): one call, and
        # the money either settled or it did not. Nothing is credited otherwise.
        receipt = _with_idem(pp.charge, usd,
                             {"user": uid, "provider": provider, "chars": chars},
                             idem=idem)
        if not receipt.ok:
            return 402, {"error": "payment refused", "detail": receipt.detail}
        credited = {}

        def credit(entry_id):
            credited["total"] = accounting.topup(
                self.conn, uid, chars, currency="chars", entry_id=entry_id,
                note="purchase %.2f USD -> %d chars (%s)"
                     % (usd, chars, receipt.reference))
        if not self._credit_once(receipt.reference, "characters", uid,
                                 receipt.reference, chars, "direct charge",
                                 credit):
            return 409, {"error": "this payment was already credited",
                         "reference": receipt.reference,
                         "balance": accounting.balance(self.conn, uid)}
        total = credited["total"]
        payload = {"ok": True, "chars": chars, "balance": total,
                   "reference": receipt.reference}
        self._idem_put(uid, idem, 200, payload)
        return 200, payload

    def credits_confirm(self, q, body):
        """The browser came back from a character purchase: read it back.

        Same honesty rule as the tier: ok only when the provider says the money
        settled. The session must belong to the caller, and a session that has
        already been credited credits nothing more - so the 200 this returns is
        the same one on a reload.
        """
        uid = self._uid(q, body)
        if not uid:
            return 401, {"error": "auth required"}
        pp = payments.get()
        fn = getattr(pp, "purchase_confirm", None) if pp is not None else None
        if not callable(fn):
            return 503, {"error": "payments not configured"}
        ref = (body or {}).get("reference") or q.get("reference") or ""
        p = fn(ref)
        if not getattr(p, "ok", False):
            return 402, {"error": "not confirmed", "detail": getattr(p, "detail", "")}
        if getattr(p, "user", "") != uid:
            return 403, {"error": "that purchase belongs to another account"}
        fresh = self._credit_once(
            p.reference, "characters", uid, p.reference, p.chars,
            "checkout confirmed",
            (lambda eid: accounting.topup(
                self.conn, uid, p.chars, currency="chars", entry_id=eid,
                note="purchase %d chars (%s)" % (p.chars, p.reference)))
            if p.chars else None)
        return 200, {"ok": True, "chars": p.chars, "credited": fresh,
                     "balance": accounting.balance(self.conn, uid),
                     "reference": p.reference}

    # ---- billing (the recurring platform tier) -------------------------------
    # The provider is the authority on plans and on whether a checkout is paid.
    # The host records only what the provider reported.

    def billing_get(self, q, body):
        uid = self._uid(q, body)
        pp = payments.get()
        plans_list = []
        fn = getattr(pp, "plans", None)
        if callable(fn):
            plans_list = [p.dict() if hasattr(p, "dict") else dict(p) for p in fn()]
        sub = billing.get(self.conn, uid) if uid else None
        return 200, {"subscription": sub,
                     "plans": plans_list,
                     "payments_configured": payments.configured(),
                     # Access is a decision, not a status word: it includes the
                     # lapsed-period check. The screen shows "manage billing"
                     # only when there is a provider customer to open.
                     "access": billing.active(self.conn, uid) if uid else False,
                     "billing_account": bool((sub or {}).get("customer"))}

    def plans_get(self, q, body):
        """The offering, for the plan screen: the tiers and the storage add-on,
        with whatever prices a payment provider reports (None until one is set)."""
        return 200, plans.catalog(payments.get())

    def billing_config(self, q, body):
        """The browser's half of checkout: a client token and the price ids.

        Public on purpose and deliberately narrow. A provider whose card form is
        a page overlay (Paddle) needs the browser to know which price to open;
        everything that can move money stays server-side. With no provider
        registered the honest answer is ok=false, and the page keeps its
        "not for sale" state instead of opening an overlay that cannot charge.
        """
        pp = payments.get()
        fn = getattr(pp, "public_config", None)
        data = fn() if callable(fn) else {
            "ok": False, "provider": "", "env": "", "prices": {},
            "currency": "USD", "detail": "no payment provider configured"}
        data["payments_configured"] = payments.configured()
        return 200, data

    def storage_get(self, q, body):
        """The signed-in user's storage: the bytes their own shelf holds and how
        much their plan plus any storage blocks include. A held book bills its
        own bytes; there is no sharing discount and nothing is auto-released.
        Read-only."""
        uid = self._uid(q, body)
        sub = billing.get(self.conn, uid) if uid else None
        plan = (sub or {}).get("plan") or ""
        blocks = billing.blocks(self.conn, uid) if uid else 0
        u = quota.usage(self.conn, self.storage, uid) if uid else {
            "user_id": uid, "own_bytes": 0, "total_bytes": 0, "blobs": 0}
        allowed = quota.allowance_bytes(plan, blocks)
        unlimited = bool(uid) and grants.storage_unlimited(self.conn, uid)
        u.update({"plan": plan, "storage_blocks": blocks,
                  "block_bytes": quota.STORAGE_BLOCK_BYTES,
                  "allowance_bytes": allowed,
                  # An admin grant lifts the refusal; the screen says so instead
                  # of showing a shelf that is over a limit nobody enforces.
                  "unlimited": unlimited,
                  "remaining_bytes": (None if unlimited
                                      else max(0, allowed - u["own_bytes"]))})
        return 200, u

    def billing_checkout(self, q, body):
        uid = self._uid(q, body)
        if not uid:
            return 401, {"error": "auth required"}
        pp = payments.get()
        fn = getattr(pp, "checkout", None)
        if not callable(fn):
            return 503, {"error": "payments not configured",
                         "detail": "the platform tier is held until a payments provider lands"}
        if not self._limit(self.buy_limiter, uid):
            return 429, {"error": "rate limited"}
        idem = (q or {}).get("_idem", "")
        hit = self._idem_get(uid, idem)
        if hit:
            return hit
        body = body or {}
        plan = body.get("plan") or q.get("plan") or ""
        items = body.get("items")
        meta = {"user": uid,
                "return_url": self._safe_return(body.get("return_url"),
                                                (q or {}).get("_base", ""))}
        if isinstance(items, list) and items:
            clean = []
            for it in items:
                if not isinstance(it, dict):
                    return 400, {"error": "bad cart item"}
                pid = str(it.get("plan") or "").strip()
                if not pid:
                    return 400, {"error": "cart item has no plan"}
                try:
                    qty = max(1, int(it.get("quantity") or 1))
                except (TypeError, ValueError):
                    return 400, {"error": "bad cart quantity"}
                clean.append({"plan": pid, "quantity": qty})
            meta["items"] = clean
            if not plan:
                plan = clean[0]["plan"]
        else:
            try:
                quantity = int(body.get("quantity") or q.get("quantity") or 1)
            except (TypeError, ValueError):
                quantity = 1
            meta["quantity"] = max(1, quantity)
        # Storage is what a plan buys; the wallet does not. A free account may
        # put dollars in the wallet and spend them on narration, but a block of
        # room is not for sale on free, because that room is the reason to move
        # up. Chris, 11-09. The screen hides the button too; this is the gate.
        wanted = [it["plan"] for it in (meta.get("items") or [])] or [plan]
        upgrades = [p for p in wanted if p not in (quota.STORAGE_ADDON, quota.FREE_PLAN)]
        sub = billing.get(self.conn, uid) or {}
        if (quota.STORAGE_ADDON in wanted and not upgrades
                and sub.get("plan") == quota.FREE_PLAN):
            return 402, {"error": "a plan is needed for storage",
                         "detail": "Storage comes with a plan. The wallet pays for "
                                   "narration on free; room on the shelf starts with "
                                   "the Reader plan."}
        co = _with_idem(fn, plan, meta, idem=idem)
        if not getattr(co, "ok", False):
            return 402, {"error": "checkout refused",
                         "detail": getattr(co, "detail", "")}
        payload = {"url": co.url, "reference": co.reference}
        self._idem_put(uid, idem, 200, payload)
        return 200, payload

    def _apply_subscription(self, uid, sub, ref, provider_id):
        return paymentapply.apply_subscription(self.conn, uid, sub, ref, provider_id)

    def billing_confirm(self, q, body):
        uid = self._uid(q, body)
        if not uid:
            return 401, {"error": "auth required"}
        pp = payments.get()
        fn = getattr(pp, "confirm", None)
        if not callable(fn):
            return 503, {"error": "payments not configured"}
        ref = (body or {}).get("reference") or q.get("reference") or ""
        sub = fn(ref)
        if not getattr(sub, "ok", False):
            return 402, {"error": "not confirmed",
                         "detail": getattr(sub, "detail", "")}
        # A session id is not a capability: without this check, anyone who
        # learned one could apply someone else's paid plan to their own account.
        if getattr(sub, "user", "") and sub.user != uid:
            return 403, {"error": "that checkout belongs to another account"}
        out = self._apply_subscription(uid, sub, ref, getattr(pp, "id", ""))
        resp = {"ok": True}
        resp.update(out)
        if "applied" in out:
            resp["subscription"] = billing.get(self.conn, uid)
        return 200, resp

    def billing_storage(self, q, body):
        """Add blocks to the storage subscription the account already has.

        Every Paddle checkout opens a new subscription, so the card form is the
        right door only for the FIRST block. A second block through the same
        door used to create a second $2 subscription and then overwrite the
        add-on row with quantity 1 - two charges, one block of room (bought
        twice in sandbox, 2026-11-13). Here the quantity is raised on the one
        subscription instead, and the provider is asked before the row moves.
        """
        uid = self._uid(q, body)
        if not uid:
            return 401, {"error": "auth required"}
        if not self._limit(self.buy_limiter, uid):
            return 429, {"error": "rate limited"}
        pp = payments.get()
        fn = getattr(pp, "add_storage", None) if pp is not None else None
        if not callable(fn):
            return 503, {"error": "payments not configured"}
        try:
            qty = int((body or {}).get("quantity") or 1)
        except (TypeError, ValueError):
            qty = 1
        qty = max(1, min(50, qty))
        sub = billing.get(self.conn, uid) or {}
        if (sub.get("plan") or "") in ("", quota.FREE_PLAN):
            return 402, {"error": "a plan is needed for storage",
                         "detail": "Storage comes with a plan. The wallet pays for "
                                   "narration on free; room on the shelf starts with "
                                   "the Reader plan."}
        a = billing.addons(self.conn, uid).get(billing.STORAGE_ADDON) or {}
        sid = (a.get("subscription_id") or "").strip()
        if not sid:
            # No block subscription yet: the card form has to open one, and that
            # is the browser's job, not this route's. The page uses this answer
            # to know which door to take.
            return 409, {"error": "no storage subscription yet",
                         "detail": "the card form opens the first block",
                         "first_block": True}
        try:
            current = int(a.get("quantity") or 0)
        except (TypeError, ValueError):
            current = 0
        res = fn(sid, current + qty)
        if not res.get("ok"):
            return 402, {"error": "storage update refused",
                         "detail": res.get("detail", "")}
        row = billing.upsert_addon(
            self.conn, uid, billing.STORAGE_ADDON,
            int(res.get("quantity") or (current + qty)),
            a.get("status") or "active", provider="paddle",
            reference=a.get("reference") or sid,
            current_period_end=a.get("current_period_end") or 0,
            customer=a.get("customer") or "", subscription_id=sid,
            cancel_at=a.get("cancel_at") or 0)
        return 200, {"ok": True, "addon": row}

    def billing_cancel(self, q, body):
        """Stop the next renewal. Access lasts to the end of the paid period.

        Without this a reader had no way to stop being billed except to ask us,
        and deleting the account did not cancel anything - the invoices kept
        coming to a customer who no longer existed. The provider is the authority:
        we ask it to stop, then record what it says.
        """
        uid = self._uid(q, body)
        if not uid:
            return 401, {"error": "auth required"}
        if not self._limit(self.buy_limiter, uid):
            return 429, {"error": "rate limited"}
        pp = payments.get()
        fn = getattr(pp, "cancel", None) if pp is not None else None
        if not callable(fn):
            return 503, {"error": "payments not configured"}
        sub = billing.get(self.conn, uid)
        sid = (sub or {}).get("subscription_id") or ""
        if not sid:
            return 409, {"error": "no subscription to cancel",
                         "detail": "this account has no recurring charge on file"}
        res = fn(sid)
        if not res.get("ok"):
            return 402, {"error": "cancel refused", "detail": res.get("detail", "")}
        if sub:
            end = res.get("current_period_end") or sub["current_period_end"]
            # Paddle keeps the subscription active and schedules the change, so
            # the row has to carry the date itself or the next load of the
            # account screen would say "renews on ..." to someone who cancelled.
            billing.upsert(self.conn, uid, sub["plan"], sub["status"],
                           provider=sub["provider"], reference=sub["reference"],
                           current_period_end=end,
                           customer=sub.get("customer", ""),
                           subscription_id=sid,
                           cancel_at=end)
            notices.notify(self.conn, uid, "billing",
                           "Renewal is stopping. You keep everything until this period ends.",
                           route="#/settings/billing")
        # Storage is what a plan buys, so a block subscription outliving its
        # plan is a charge for room the account is no longer allowed to buy.
        # Stopping it here keeps the two in step; a refusal is reported and
        # does not undo the plan cancel.
        storage_cancel = {}
        a = billing.addons(self.conn, uid).get(billing.STORAGE_ADDON) or {}
        asid = (a.get("subscription_id") or "").strip()
        if asid:
            r2 = fn(asid)
            storage_cancel = {"ok": bool(r2.get("ok")),
                              "detail": r2.get("detail", "")}
            if r2.get("ok"):
                end2 = r2.get("current_period_end") or a.get("current_period_end") or 0
                billing.upsert_addon(
                    self.conn, uid, billing.STORAGE_ADDON,
                    a.get("quantity") or 0, a.get("status") or "",
                    provider=a.get("provider") or "paddle",
                    reference=a.get("reference") or asid,
                    current_period_end=end2, customer=a.get("customer") or "",
                    subscription_id=asid, cancel_at=end2)
        return 200, {"ok": True, "cancel_at_period_end":
                     bool(res.get("cancel_at_period_end")),
                     "current_period_end": res.get("current_period_end", 0),
                     "storage_cancel": storage_cancel,
                     "subscription": billing.get(self.conn, uid)}

    def billing_portal(self, q, body):
        """A provider-hosted page to change the card or the plan.

        Card details never reach this server, and a plan change is priced by the
        provider from its own catalog. Needs the customer id, which is why the
        checkout now stores it.
        """
        uid = self._uid(q, body)
        if not uid:
            return 401, {"error": "auth required"}
        if not self._limit(self.buy_limiter, uid):
            return 429, {"error": "rate limited"}
        pp = payments.get()
        fn = getattr(pp, "portal", None) if pp is not None else None
        if not callable(fn):
            return 503, {"error": "payments not configured"}
        sub = billing.get(self.conn, uid)
        customer = (sub or {}).get("customer") or ""
        if not customer:
            return 409, {"error": "no billing customer on file"}
        res = fn(customer, self._safe_return((body or {}).get("return_url"),
                                             (q or {}).get("_base", "")))
        if not res.get("ok"):
            return 402, {"error": "portal refused", "detail": res.get("detail", "")}
        return 200, {"ok": True, "url": res.get("url", "")}

    def billing_change(self, q, body):
        """Move a paid subscription onto another tier, without a second one.

        Buying a plan through checkout while already subscribed would open a
        second subscription, which is two charges and two cancels to think
        about. The provider changes the existing one, and the webhook it sends
        back is what writes the new tier here.
        """
        uid = self._uid(q, body)
        if not uid:
            return 401, {"error": "auth required"}
        if not self._limit(self.buy_limiter, uid):
            return 429, {"error": "rate limited"}
        pp = payments.get()
        fn = getattr(pp, "change_plan", None)
        if not callable(fn):
            return 503, {"error": "payments not configured"}
        sub = billing.get(self.conn, uid)
        sid = (sub or {}).get("subscription_id") or ""
        if not sid:
            return 409, {"error": "no subscription to change",
                         "detail": "pick a plan first; a change moves the one you have"}
        plan = ((body or {}).get("plan") or "").strip()
        if plan not in quota.PAID_TIERS:
            return 400, {"error": "not a paid plan", "detail": plan}
        if plan == sub["plan"]:
            return 409, {"error": "already on that plan"}
        if sub.get("cancel_at"):
            # Changing items on a subscription that is scheduled to end would
            # charge a prorated amount for a period it is not going to have.
            return 409, {"error": "renewal is stopping",
                         "detail": "keep the renewal going first, then change plan"}
        res = fn(sid, plan, (body or {}).get("cycle") or "")
        if not res.get("ok"):
            return 402, {"error": "change refused",
                         "detail": res.get("detail", "")}
        notices.notify(self.conn, uid, "billing",
                       "Your plan is now %s. The difference was prorated today." % plan,
                       route="#/settings/billing")
        return 200, {"ok": True, "plan": plan,
                     "cycle": res.get("cycle", ""),
                     "subscription": billing.get(self.conn, uid)}

    def billing_resume(self, q, body):
        """Put the next renewal back after a scheduled cancel."""
        uid = self._uid(q, body)
        if not uid:
            return 401, {"error": "auth required"}
        if not self._limit(self.buy_limiter, uid):
            return 429, {"error": "rate limited"}
        pp = payments.get()
        fn = getattr(pp, "resume", None)
        if not callable(fn):
            return 503, {"error": "payments not configured"}
        sub = billing.get(self.conn, uid)
        sid = (sub or {}).get("subscription_id") or ""
        if not sid:
            return 409, {"error": "no subscription on file"}
        if not sub.get("cancel_at"):
            return 200, {"ok": True, "detail": "nothing was scheduled to stop"}
        res = fn(sid)
        if not res.get("ok"):
            return 402, {"error": "resume refused",
                         "detail": res.get("detail", "")}
        # The provider said the scheduled change is gone, so record that.
        billing.upsert(self.conn, uid, sub["plan"], sub["status"],
                       provider=sub["provider"], reference=sub["reference"],
                       current_period_end=sub["current_period_end"],
                       customer=sub.get("customer", ""),
                       subscription_id=sid, cancel_at=0)
        notices.notify(self.conn, uid, "billing", "Your plan keeps renewing.",
                       route="#/settings/billing")
        return 200, {"ok": True, "subscription": billing.get(self.conn, uid)}

    def _apply_subscription_state(self, st, provider_id):
        return paymentapply.apply_subscription_state(self.conn, st, provider_id)

    def _credit_wallet_tx(self, pp, tx, cust=None):
        return paymentapply.credit_wallet_tx(self.conn, pp, tx, cust)

    def payment_webhook(self, raw, sig_header, provider_id="stripe"):
        """Stripe's server-to-server view of a settled checkout.

        Public by necessity (Stripe cannot hold a session), so the signature is
        the whole authentication: nothing is read out of the body until the
        provider verifies it. On a completed checkout the session is re-read
        from Stripe and applied exactly as /billing/confirm applies it, so a
        buyer who closes the tab still gets their plan. Replays are harmless -
        the apply is an upsert and Stripe may retry.
        """
        pp = payments.get(provider_id)
        if pp is None:
            return 404, {"error": "unknown payment provider"}
        verify = getattr(pp, "verify_webhook", None)
        if not callable(verify):
            return 501, {"error": "provider has no webhook support"}
        event, err = verify(raw, sig_header)
        if event is None:
            return 400, {"error": "bad webhook", "detail": err}
        norm = getattr(pp, "normalize_event", None)
        if callable(norm):
            # A provider whose events ARE the state (Paddle) hands the core one
            # normalized shape and the core mirrors it. There is no session to
            # re-read and no price is computed here, only what Paddle said.
            # Idempotency is the event id, so a retry or a replay is a no-op.
            out = billing.apply_event(self.conn, norm(event) or {})
            if out.get("kind") == "transaction" and out.get("transaction"):
                out["wallet"] = self._credit_wallet_tx(pp, out["transaction"],
                                                       out.get("customer"))
            return 200, out
        ref = ""
        rfn = getattr(pp, "webhook_reference", None)
        if callable(rfn):
            ref = rfn(event) or ""
        typ = (event or {}).get("type", "")
        obj = ((event or {}).get("data") or {}).get("object") or {}
        # A character purchase settles into a payment-mode session. Credit it
        # here so a buyer who closes the tab is not charged for nothing.
        if ref and (obj.get("metadata") or {}).get("kind") == "characters":
            pfn = getattr(pp, "purchase_confirm", None)
            if not callable(pfn):
                return 200, {"ok": True, "reference": ref, "applied": False,
                             "detail": "provider cannot confirm purchases"}
            p = pfn(ref)
            if not getattr(p, "ok", False):
                return 200, {"ok": True, "reference": ref, "applied": False,
                             "detail": getattr(p, "detail", "")}
            fresh = self._credit_once(
                p.reference, "characters", p.user, p.reference, p.chars,
                "webhook",
                (lambda eid: accounting.topup(
                    self.conn, p.user, p.chars, currency="chars", entry_id=eid,
                    note="purchase %d chars (%s)" % (p.chars, p.reference)))
                if p.chars and p.user else None)
            return 200, {"ok": True, "reference": ref, "applied": fresh,
                         "user": p.user, "chars": p.chars}
        if not ref:
            # Renewals, failed cards, cancellations: no checkout session, but a
            # subscription id, which is the row we must update or the paid tier
            # never lapses.
            sfn = getattr(pp, "subscription_state", None)
            st = sfn(event) if callable(sfn) else None
            if st:
                return self._apply_subscription_state(st, getattr(pp, "id", ""))
            return 200, {"ok": True, "ignored": typ}
        sub = pp.confirm(ref)
        if not getattr(sub, "ok", False):
            # Not paid / not complete: acknowledge so Stripe stops retrying, but
            # record nothing. Money that has not settled is not a sale.
            return 200, {"ok": True, "reference": ref, "applied": False,
                         "detail": getattr(sub, "detail", "")}
        uid = getattr(sub, "user", "") or ""
        if not uid:
            return 200, {"ok": True, "reference": ref, "applied": False,
                         "detail": "no user on checkout session"}
        self._apply_subscription(uid, sub, ref, getattr(pp, "id", ""))
        return 200, {"ok": True, "reference": ref, "applied": True, "user": uid}
