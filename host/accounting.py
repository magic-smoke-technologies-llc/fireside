"""Plugin-led accounting: reserve, then settle on the plugin's report.

The plugin is the sole authority on counts, price, eligibility and the external
balance. The host never infers a charge from a successful request or from saved
audio, and never turns `unknown` into zero.

Only ONE designated connection sells Fireside credits: a (provider, voice,
model) triple. Every other connection is pass-through - no reserve, no debit.
`unknown`/`pending` a report leaves the reservation standing and visibly
pending; it is never guessed or auto-released. A later reconciliation settles it
without a duplicate debit.
"""

import sqlite3
import uuid

from host.db import now

_OPEN = ("reserved", "pending")        # reservation states still holding credits


class Insufficient(Exception):
    """The reservation would overdraw the user's Fireside balance."""


def sale_connection_matches(spec, provider, voice, model, connection_id=None):
    """True if this render goes through the ONE designated sale connection.

    When the switch names a connection id, that id is the whole test: every
    voice on the sale connection is resold, and a render through any other
    connection (a BYO key, the local engine) is never metered even if it names
    the same voice. Without an id (the shorthand spec) it falls back to the
    (provider, voice, model) triple.
    """
    if not spec:
        return False
    scid = spec.get("connection_id")
    if scid:
        return scid == (connection_id or "")
    if spec.get("provider") != provider:
        return False
    if spec.get("voice") != voice:
        return False
    m = spec.get("model") or ""
    return (not m) or (m == (model or ""))


def balance(conn, user_id):
    r = conn.execute("SELECT amount FROM balances WHERE user_id=?", (user_id,)).fetchone()
    return float(r["amount"]) if r else 0.0


def reserved_total(conn, user_id):
    r = conn.execute(
        "SELECT COALESCE(SUM(amount),0) s FROM reservations WHERE user_id=? AND state IN (?,?)",
        (user_id,) + _OPEN).fetchone()
    return float(r["s"])


def available(conn, user_id):
    return balance(conn, user_id) - reserved_total(conn, user_id)


def topup(conn, user_id, amount, currency="USD", note=""):
    conn.execute("INSERT OR IGNORE INTO balances(user_id,currency,amount) VALUES(?,?,0)",
                 (user_id, currency))
    conn.execute("UPDATE balances SET amount=amount+?,currency=? WHERE user_id=?",
                 (float(amount), currency, user_id))
    _entry(conn, user_id, "topup", float(amount), currency, None, None, note)
    conn.commit()
    return balance(conn, user_id)


def reserve(conn, user_id, amount, currency="USD", job_id=None, note=""):
    """Atomically hold `amount`. Raises Insufficient rather than overdrawing."""
    amount = float(amount)
    if amount <= 0:
        raise ValueError("reserve amount must be positive")
    # A SAVEPOINT, not BEGIN IMMEDIATE: the host serves requests from a thread
    # pool over one shared connection, so a transaction opened here is the same
    # transaction another thread is writing into. BEGIN IMMEDIATE raises
    # "cannot start a transaction within a transaction" the moment a concurrent
    # write is pending, and the ROLLBACK that used to follow discarded that
    # other thread's work along with ours (a session row not yet committed, so
    # the cookie the client already holds stops resolving). A savepoint nests,
    # and rolling back to it undoes only what this reservation did.
    save = "reserve_" + uuid.uuid4().hex[:12]
    conn.execute("SAVEPOINT " + save)
    try:
        avail = available(conn, user_id)
        if avail < amount:
            raise Insufficient("available %.4f < %.4f" % (avail, amount))
        rid = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO reservations(id,user_id,amount,currency,state,job_id,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (rid, user_id, amount, currency, "reserved", job_id or "", now(), now()))
    except Exception:
        conn.execute("ROLLBACK TO " + save)
        conn.execute("RELEASE " + save)
        raise
    conn.execute("RELEASE " + save)
    conn.commit()
    return rid


def settle(conn, res_id, report):
    """Apply the plugin's report to a reservation. Returns the final state."""
    res = _get(conn, res_id)
    if res["state"] not in _OPEN:
        return res["state"]                      # already terminal; no double apply
    charge = getattr(report, "charge", "unknown")
    cost = getattr(report, "cost", None)
    currency = getattr(report, "currency", "") or res["currency"]
    if charge == "charged" and cost is not None:
        _debit(conn, res["user_id"], float(cost), currency, res)
        _set_state(conn, res_id, "settled")
        conn.commit()
        return "settled"
    # unknown or pending: hold the reservation, visibly pending
    _set_state(conn, res_id, "pending")
    conn.commit()
    return "pending"


def reconcile(conn, res_id, report):
    """Settle a pending reservation later. No duplicate debit for a settled one."""
    return settle(conn, res_id, report)


def release(conn, res_id, reason="released"):
    """Return held credits to the user (a run that never happened)."""
    res = _get(conn, res_id)
    if res["state"] in _OPEN:
        _set_state(conn, res_id, "released")
        _entry(conn, res["user_id"], "release", 0.0, res["currency"], None,
               None, reason)
        conn.commit()
    return "released"


def ledger_entries(conn, user_id):
    rows = conn.execute("SELECT * FROM ledger WHERE user_id=? ORDER BY created_at",
                        (user_id,))
    return [dict(r) for r in rows]


def reservation(conn, res_id):
    return _get(conn, res_id)


# ---- internals -----------------------------------------------------------------

def _get(conn, res_id):
    r = conn.execute("SELECT * FROM reservations WHERE id=?", (res_id,)).fetchone()
    if not r:
        raise KeyError("no reservation %s" % res_id)
    return dict(r)


def _set_state(conn, res_id, state):
    conn.execute("UPDATE reservations SET state=?,updated_at=? WHERE id=?",
                 (state, now(), res_id))


def _debit(conn, user_id, cost, currency, res):
    conn.execute("INSERT OR IGNORE INTO balances(user_id,currency,amount) VALUES(?,?,0)",
                 (user_id, currency))
    conn.execute("UPDATE balances SET amount=amount-? WHERE user_id=?",
                 (cost, user_id))
    _entry(conn, user_id, "charge", -cost, currency, None, res.get("job_id"),
           "reservation %s" % res["id"])


def _entry(conn, user_id, kind, delta, currency, book_id, job_id, note):
    conn.execute(
        "INSERT INTO ledger(id,user_id,kind,delta,currency,book_id,job_id,note,created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (uuid.uuid4().hex, user_id, kind, delta, currency, book_id or "",
         job_id or "", note or "", now()))
