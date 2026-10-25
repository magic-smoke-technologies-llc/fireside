"""Recurring platform subscriptions, kept on the server.

The platform tier is separate from character capacity: it pays for the server and
storage and buys nothing else (docs/PRICING.md). The host holds only the state the
payment provider reported - it never computes a price or a period. One active
subscription per user for now; the provider is the authority on status.

Add-ons are separate rows: a storage block is a monthly thing you buy N of, and
it adds bytes to whichever tier you are on instead of replacing it. The provider
is still the authority on status and period; this module holds the count.
"""

import time

from host.db import now

STORAGE_ADDON = "storage"

# How long past its paid period a row stays "active" before the host decides the
# provider has stopped telling it anything. Webhooks are the authority, but a
# webhook that never arrives (an endpoint deleted, a delivery outage, a plan
# cancelled from the Stripe dashboard) must not leave a paid tier running
# forever. Three days is long enough to survive a delivery outage and short
# enough that a lapsed charge does not buy a whole extra month.
LAPSE_GRACE_SECONDS = 3 * 86400


def get(conn, uid):
    r = conn.execute("SELECT * FROM subscriptions WHERE user_id=?",
                     (uid,)).fetchone()
    if not r:
        return None
    return {"user_id": r["user_id"], "plan": r["plan"], "status": r["status"],
            "provider": r["provider"], "reference": r["reference"],
            "current_period_end": r["current_period_end"],
            "customer": r["customer"] or "",
            "subscription_id": r["subscription_id"] or "",
            "updated_at": r["updated_at"]}


def upsert(conn, uid, plan, status, provider="", reference="",
           current_period_end=0, customer="", subscription_id=""):
    """Record what the provider said. Merges into the one row a user has.

    A lifecycle event often knows the status but not the checkout session, and a
    checkout knows both. An empty value therefore never overwrites a stored one:
    `excluded` is only taken when it is non-empty, so a renewal cannot blank the
    ids that make a later cancel possible.
    """
    conn.execute(
        "INSERT INTO subscriptions (user_id, plan, status, provider, reference, "
        "current_period_end, customer, subscription_id, updated_at, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET plan=excluded.plan, "
        "status=excluded.status, provider=excluded.provider, "
        "reference=COALESCE(NULLIF(excluded.reference, ''), subscriptions.reference), "
        "current_period_end=excluded.current_period_end, "
        "customer=COALESCE(NULLIF(excluded.customer, ''), subscriptions.customer), "
        "subscription_id=COALESCE(NULLIF(excluded.subscription_id, ''), "
        "subscriptions.subscription_id), "
        "updated_at=excluded.updated_at",
        (uid, plan, status, provider, reference, int(current_period_end or 0),
         customer or "", subscription_id or "", now(), now()))
    conn.commit()
    return get(conn, uid)


def by_subscription(conn, subscription_id):
    """The account a provider subscription belongs to, or "".

    A renewal or a failed payment names the subscription, never our user id, so
    this is the only way back to a row. Checked against both tables because a
    cart's storage add-on is written on its own row.
    """
    sid = (subscription_id or "").strip()
    if not sid:
        return ""
    for table in ("subscriptions", "addons"):
        r = conn.execute("SELECT user_id FROM %s WHERE subscription_id=?" % table,
                         (sid,)).fetchone()
        if r:
            return r["user_id"]
    return ""


def _lapsed(period_end, now_ts=None):
    """True when a paid period ended long enough ago that the row is stale."""
    try:
        end = int(period_end or 0)
    except (TypeError, ValueError):
        return False
    if end <= 0:
        # no period recorded (the free tier, or a grant): nothing to expire.
        return False
    ts = time.time() if now_ts is None else now_ts
    return ts > end + LAPSE_GRACE_SECONDS


def active(conn, uid, now_ts=None):
    """True when the user's subscription is currently paid/active.

    Status alone is not enough: `current_period_end` is what the provider's
    renewal would have moved, and nothing renews if the webhook never lands. A
    row whose period ended more than the grace window ago is not active, whatever
    status it still carries.
    """
    s = get(conn, uid)
    if not s or s["status"] not in ("active", "trialing"):
        return False
    return not _lapsed(s["current_period_end"], now_ts)


def assign_free(conn, uid):
    """Put a new account on the free tier, once.

    Called when a user row is first created so sign-ups start at 500 MB. An
    account that already has any subscription row is left alone, and a missing
    row keeps failing open to the platform allowance (quota.allowance_bytes), so
    accounts that predate the free tier are not silently cut down to 500 MB.
    """
    if get(conn, uid) is not None:
        return get(conn, uid)
    return upsert(conn, uid, "free", "active", provider="", reference="")


def addons(conn, uid):
    """{addon: row} for the user's add-ons."""
    out = {}
    for r in conn.execute("SELECT * FROM addons WHERE user_id=?", (uid,)):
        out[r["addon"]] = {"addon": r["addon"], "quantity": r["quantity"],
                           "status": r["status"], "provider": r["provider"],
                           "reference": r["reference"],
                           "current_period_end": r["current_period_end"],
                           "customer": r["customer"] or "",
                           "subscription_id": r["subscription_id"] or ""}
    return out


def upsert_addon(conn, uid, addon, quantity, status, provider="", reference="",
                 current_period_end=0, customer="", subscription_id=""):
    """Record how many of an add-on the provider says are paid for."""
    conn.execute(
        "INSERT INTO addons (user_id, addon, quantity, status, provider, "
        "reference, current_period_end, customer, subscription_id, updated_at, "
        "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(user_id, addon) DO UPDATE SET quantity=excluded.quantity, "
        "status=excluded.status, provider=excluded.provider, "
        "reference=COALESCE(NULLIF(excluded.reference, ''), addons.reference), "
        "current_period_end=excluded.current_period_end, "
        "customer=COALESCE(NULLIF(excluded.customer, ''), addons.customer), "
        "subscription_id=COALESCE(NULLIF(excluded.subscription_id, ''), "
        "addons.subscription_id), "
        "updated_at=excluded.updated_at",
        (uid, addon, int(quantity or 0), status, provider, reference,
         int(current_period_end or 0), customer or "", subscription_id or "",
         now(), now()))
    conn.commit()
    return addons(conn, uid).get(addon)


def blocks(conn, uid):
    """Paid storage blocks the account currently holds (an active add-on)."""
    a = addons(conn, uid).get(STORAGE_ADDON)
    if not a or a["status"] not in ("active", "trialing"):
        return 0
    if _lapsed(a["current_period_end"]):
        return 0
    try:
        return max(0, int(a["quantity"] or 0))
    except (TypeError, ValueError):
        return 0


def list_all(conn):
    """{user_id: row} for every account with a subscription row. An account
    with no row is on no tier here and fails open to the platform allowance."""
    out = {}
    for r in conn.execute("SELECT * FROM subscriptions"):
        out[r["user_id"]] = {"user_id": r["user_id"], "plan": r["plan"],
                             "status": r["status"], "provider": r["provider"],
                             "reference": r["reference"],
                             "current_period_end": r["current_period_end"],
                             "customer": r["customer"] or "",
                             "subscription_id": r["subscription_id"] or ""}
    return out
