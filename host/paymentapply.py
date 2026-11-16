"""Applying what a payment provider reported to the rows the host reads.

Plain functions over a connection, split out of host/app.py. Each is the single
place one kind of provider event becomes ledger or subscription rows, and each is
safe to run twice: a webhook retry and the browser's own confirm can both land.
"""

import sys

from host import accounting, billing, db, quota


def claim_payment(conn, event_id, kind, uid, reference, chars=0, detail=""):
    """True the first time this provider event is applied, False after.

    One row per provider object, so a webhook retry and the browser's own
    confirm can both land and the characters are credited once. Portable
    across the two engines: a select then an insert, no upsert clause.

    Only a duplicate key means "already applied". Any other failure (a
    dropped connection, a full disk) raises, so a webhook answers 500 and
    the provider retries; reporting it as "already credited" would tell the
    provider to stop and leave a paying customer with no balance.
    """
    event_id = (event_id or "").strip()
    if not event_id:
        return False
    if conn.execute("SELECT 1 FROM payment_events WHERE id=?",
                         (event_id,)).fetchone():
        return False
    try:
        conn.execute(
            "INSERT INTO payment_events (id, kind, user_id, reference, "
            "chars, detail, created_at) VALUES (?,?,?,?,?,?,?)",
            (event_id, kind, uid, reference, int(chars or 0),
             detail or "", db.now()))
    except Exception as e:
        if db.is_duplicate(e):
            return False          # another thread claimed it between the two
        raise
    return True


def credit_once(conn, event_id, kind, uid, reference, chars, detail, credit):
    """Claim a provider event and run `credit()` for it, once.

    `credit(entry_id)` must post a ledger row under that id: the ledger's own
    primary key then refuses a second posting, so releasing the claim after
    a failure that happened to land (a commit that errored late) cannot pay
    twice. True when this call did the crediting. If `credit` fails the claim
    is released before the error goes on, so the retry is not told "already
    credited" for money that never landed.
    """
    if not claim_payment(conn, event_id, kind, uid, reference, chars, detail):
        return False
    if credit is not None:
        try:
            credit("pay:" + event_id.strip())
        except Exception as e:
            if db.is_duplicate(e):
                # The ledger holds this payment but our claim was new: an
                # earlier attempt wrote the ledger row and then failed before
                # the balance moved (only possible on MySQL, which cannot roll
                # the pair back). Do not pay twice; say so, so support can check
                # the balance and restore it with adjust().
                print("fireside: ALERT payment %s is in the ledger but its "
                      "claim was released; check the balance of %s"
                      % (event_id.strip(), uid), file=sys.stderr)
                return False
            try:
                conn.execute("DELETE FROM payment_events WHERE id=?",
                             (event_id.strip(),))
                conn.commit()
            except Exception:
                pass        # keep the original failure, not this one
            raise
    return True


def apply_subscription(conn, uid, sub, ref, provider_id):
    """Write what the provider reported into the rows the host reads.

    A cart applies each item to its own row; a single purchase is either the
    tier or the storage add-on. Idempotent by construction (both writers are
    upserts), so the browser confirm and the webhook landing together - in
    either order - leave the same rows, never a doubled one.
    """
    ids = {"provider": provider_id, "reference": ref,
           "current_period_end": sub.current_period_end,
           "customer": getattr(sub, "customer", "") or "",
           "subscription_id": getattr(sub, "subscription_id", "") or ""}
    items = getattr(sub, "items", None)
    if items:
        applied = []
        for it in items:
            pid = (it or {}).get("plan") or ""
            qty = int((it or {}).get("quantity") or 1)
            if pid == quota.STORAGE_ADDON:
                row = billing.upsert_addon(
                    conn, uid, quota.STORAGE_ADDON, qty, sub.status,
                    **ids)
                applied.append({"addon": row})
            elif pid:
                row = billing.upsert(conn, uid, pid, sub.status, **ids)
                applied.append({"subscription": row})
        return {"applied": applied}
    # An add-on does not replace the tier - it adds capacity to it.
    if getattr(sub, "plan", "") == quota.STORAGE_ADDON:
        return {"addon": billing.upsert_addon(
            conn, uid, quota.STORAGE_ADDON,
            getattr(sub, "quantity", 0) or 1, sub.status, **ids)}
    if not getattr(sub, "plan", ""):
        # a paid session that names no tier: a character purchase, or a
        # shape we do not understand. Either way a blank tier row is not a
        # subscription, and writing one would grant a free platform plan.
        return {"ignored": "checkout names no plan"}
    return {"subscription": billing.upsert(
        conn, uid, sub.plan, sub.status, **ids)}


def apply_subscription_state(conn, st, provider_id):
    """Apply a lifecycle event to the row that names that subscription.

    Matched by the provider's subscription id, never by user: a renewal body
    carries no user id, and guessing one from a customer id would be worse
    than doing nothing. An event for a subscription we never stored is
    acknowledged and ignored.
    """
    sid = (st or {}).get("subscription_id", "")
    uid = billing.by_subscription(conn, sid)
    if not uid:
        return 200, {"ok": True, "applied": False,
                     "detail": "unknown subscription", "ignored": st.get("event", "")}
    status = st.get("status") or ""
    end = int(st.get("current_period_end") or 0)
    # A scheduled cancel lives in the future while the row is still active.
    # Read it from the event, so the screen can say the next charge is off
    # without asking the provider anything.
    cancel_at = (int(st.get("scheduled_change_at") or 0)
                 if st.get("scheduled_change_action") == "cancel" else 0)
    row = billing.get(conn, uid)
    if row and row.get("subscription_id") == sid:
        billing.upsert(conn, uid, row["plan"], status or row["status"],
                       provider=provider_id, reference=row["reference"],
                       current_period_end=end or row["current_period_end"],
                       customer=st.get("customer", ""), subscription_id=sid,
                       cancel_at=cancel_at)
    addon = billing.addons(conn, uid).get(billing.STORAGE_ADDON)
    if addon and addon.get("subscription_id") == sid:
        billing.upsert_addon(conn, uid, billing.STORAGE_ADDON,
                             addon["quantity"], status or addon["status"],
                             provider=provider_id,
                             reference=addon["reference"],
                             current_period_end=end or addon["current_period_end"],
                             customer=st.get("customer", ""),
                             subscription_id=sid, cancel_at=cancel_at)
    return 200, {"ok": True, "applied": True, "user": uid,
                 "status": status, "event": st.get("event", "")}


def credit_wallet_tx(conn, pp, tx, cust=None):
    """Credit the wallet for a one-time load Paddle says was paid.

    A wallet top-up is not a subscription: no renewal will ever come, and
    there is no checkout session for /credits/confirm to read back. So this
    delivery is the only receipt that will exist, and without it a buyer
    pays and their balance never moves. Credited once per transaction id;
    an unknown price credits nothing (a wrong amount here would hand out
    money the card was never charged).
    """
    tx = tx or {}
    if (tx.get("status") or "") != "completed":
        return {"credited": False, "detail": "not completed"}
    tid = (tx.get("transaction_id") or "").strip()
    if not tid:
        return {"credited": False, "detail": "no transaction id"}
    cents = 0
    fn = getattr(pp, "wallet_amount", None)
    if callable(fn):
        try:
            cents = int(fn(tx.get("price_id") or "") or 0)
        except (TypeError, ValueError):
            cents = 0
    if cents <= 0:
        return {"credited": False, "detail": "not a wallet price"}
    uid = (tx.get("user_id") or "").strip()
    if not uid:
        # The overlay sets our key on the transaction, so this is the rare
        # case of a load bought from a Paddle-hosted page; the customer was
        # linked by this same event a moment ago.
        c = billing.customer_by_id(conn,
                                   (tx.get("customer_id")
                                    or (cust or {}).get("customer_id") or ""))
        uid = (c or {}).get("user_id") or ""
    if not uid:
        return {"credited": False, "detail": "transaction names no account"}
    usd = cents / 100.0
    if not credit_once(conn, 
            tid, "wallet", uid, tid, cents, "paddle transaction",
            lambda eid: accounting.topup(
                conn, uid, usd, currency="USD", entry_id=eid,
                note="wallet +$%.2f (%s)" % (usd, tid))):
        return {"credited": False, "detail": "already credited", "user": uid}
    return {"credited": True, "user": uid, "usd": usd}
