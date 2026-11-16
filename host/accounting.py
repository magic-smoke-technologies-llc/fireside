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

from host.db import (now, savepoint, release_savepoint, exclusive,
                     rollback_to_savepoint)

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


def currency(conn, user_id):
    """The unit this user's balance is held in, or "" for an account with no
    balance row yet. Read it instead of assuming - a cutover leaves both
    units live until the last row moves."""
    r = conn.execute("SELECT currency FROM balances WHERE user_id=?",
                     (user_id,)).fetchone()
    return (r["currency"] or "") if r else ""


def absorbed(conn, user_id):
    """Ledger rows where a run cost more than we held and we ate the gap. The
    wallet history shows these with estimate vs final; anything else here is a
    bug wearing an overage's clothes."""
    rows = conn.execute(
        "SELECT * FROM ledger WHERE user_id=? AND note LIKE '%absorbed overage%'"
        " ORDER BY created_at", (user_id,))
    return [dict(r) for r in rows]


def reserved_total(conn, user_id):
    r = conn.execute(
        "SELECT COALESCE(SUM(amount),0) s FROM reservations WHERE user_id=? AND state IN (?,?)",
        (user_id,) + _OPEN).fetchone()
    return float(r["s"])


def available(conn, user_id):
    return balance(conn, user_id) - reserved_total(conn, user_id)


def topup(conn, user_id, amount, currency="USD", note="", entry_id=""):
    """Add credit. `entry_id` makes the credit idempotent: the ledger row's id is
    the primary key, so a second call with the same id raises IntegrityError
    instead of paying twice (a retried webhook, a re-read of the same period)."""
    # The ledger row and the balance move together or not at all. On SQLite a
    # savepoint makes that true (and, as in reserve(), undoes only this credit on
    # the shared connection). On MySQL, which runs autocommit, db.savepoint writes
    # nothing, so the order carries it: the ledger row first. A repeated
    # entry_id then raises before the balance has moved, and a failure after the
    # ledger row leaves a row with no balance change - a credit that is missing
    # and visible in the ledger, which support can restore with adjust(), rather
    # than one that is paid twice. credit_once() logs that case loudly.
    save = "topup_" + uuid.uuid4().hex[:12]
    with exclusive(conn):         # from SAVEPOINT to commit, no other thread's savepoint
        savepoint(conn, save)
        try:
            conn.execute("INSERT OR IGNORE INTO balances(user_id,currency,amount) "
                         "VALUES(?,?,0)", (user_id, currency))
            _entry(conn, user_id, "topup", float(amount), currency, None, None,
                   note, entry_id=entry_id)
            conn.execute("UPDATE balances SET amount=amount+?,currency=? "
                         "WHERE user_id=?", (float(amount), currency, user_id))
        except Exception:
            rollback_to_savepoint(conn, save)
            raise
        release_savepoint(conn, save)
        conn.commit()
    return balance(conn, user_id)


class BelowZero(Exception):
    """An adjustment would take the balance under zero."""


def adjust(conn, user_id, amount, note="", who=""):
    """Add or remove credit by hand, for support and comps.

    Signed like the ledger: +5.00 adds, -5.00 removes. A removal that would
    take the balance below zero is refused, not clamped and not allowed: a
    negative balance is the signature of an absorbed overage, and an admin
    debit is not one. The row says who did it and why, so a balance that moved
    without a card payment always has a name on it.
    """
    amount = float(amount)
    if amount == 0:
        raise ValueError("amount must not be zero")
    cur = currency(conn, user_id) or "USD"
    held = balance(conn, user_id)
    if held + amount < 0:
        raise BelowZero("balance %.2f, cannot remove %.2f" % (held, abs(amount)))
    conn.execute("INSERT OR IGNORE INTO balances(user_id,currency,amount) VALUES(?,?,0)",
                 (user_id, cur))
    conn.execute("UPDATE balances SET amount=amount+? WHERE user_id=?",
                 (amount, user_id))
    _entry(conn, user_id, "adjust", amount, cur, None, None, note)
    conn.commit()
    return balance(conn, user_id)


def reserve(conn, user_id, amount, currency="USD", job_id=None, note=""):
    """Atomically hold `amount`. Raises Insufficient rather than overdrawing."""
    amount = float(amount)
    if amount <= 0:
        raise ValueError("reserve amount must be positive")
    # A SAVEPOINT, not BEGIN IMMEDIATE: the host serves requests from a thread
    # pool over one shared connection, so a transaction opened here is the same
    # transaction another thread is writing into. (On MySQL, which runs with
    # autocommit on, db.savepoint writes nothing: the savepoint cannot survive
    # the statement that sets it. The read-then-insert below is still guarded by
    # the balance UPDATE, which is atomic on its own.) BEGIN IMMEDIATE raises
    # "cannot start a transaction within a transaction" the moment a concurrent
    # write is pending, and the ROLLBACK that used to follow discarded that
    # other thread's work along with ours (a session row not yet committed, so
    # the cookie the client already holds stops resolving). A savepoint nests,
    # and rolling back to it undoes only what this reservation did.
    save = "reserve_" + uuid.uuid4().hex[:12]
    with exclusive(conn):         # no other thread's savepoint between ours and the commit
        savepoint(conn, save)
        try:
            avail = available(conn, user_id)
            if avail < amount:
                raise Insufficient("available %.4f < %.4f" % (avail, amount))
            rid = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO reservations(id,user_id,amount,currency,state,job_id,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (rid, user_id, amount, currency, "reserved", job_id or "", now(),
                 now()))
        except Exception:
            rollback_to_savepoint(conn, save)
            raise
        release_savepoint(conn, save)
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
    """Debit the settled cost. Never clamps.

    The only legitimate way a balance goes negative is a final cost above the
    estimate we held, and we absorb that: the run already happened, so
    refusing the debit would leave the ledger disagreeing with reality. The
    row therefore says so, with both numbers, and an `absorbed` charge is
    greppable - a negative balance without one is a bug.
    """
    held = float(res.get("amount") or 0.0)
    note = "reservation %s" % res["id"]
    if cost > held:
        note = ("reservation %s absorbed overage %.2f: estimate %.2f final %.2f"
                % (res["id"], cost - held, held, cost))
    conn.execute("INSERT OR IGNORE INTO balances(user_id,currency,amount) VALUES(?,?,0)",
                 (user_id, currency))
    conn.execute("UPDATE balances SET amount=amount-? WHERE user_id=?",
                 (cost, user_id))
    _entry(conn, user_id, "charge", -cost, currency, None, res.get("job_id"),
           note)


def _entry(conn, user_id, kind, delta, currency, book_id, job_id, note,
           entry_id=""):
    conn.execute(
        "INSERT INTO ledger(id,user_id,kind,delta,currency,book_id,job_id,note,created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (entry_id or uuid.uuid4().hex, user_id, kind, delta, currency, book_id or "",
         job_id or "", note or "", now()))


def settle_run(conn, storage, speech, book_id, todo, rules, voice, model, speed,
               res_id, fresh, cred, job_id):
    """Charge a finished run for the text it actually voiced.

    One implementation for the two places a run can end: the request process
    that started it, and a render job in its own process (python3 -m
    host.renderjob) that picked the row up later. The reservation was made by
    the request and remembered on the job row, so this only has to do the same
    arithmetic in both places. settle() ignores a reservation that is already
    terminal, so a retried run cannot charge twice.
    """
    from host import jobs as _jobs          # local: accounting must stay importable
    if not res_id:
        return None
    try:
        remaining = _jobs.stale_book_chars(conn, storage, speech, book_id, todo,
                                           rules, voice, model, speed)
    except Exception:
        remaining = 0
    actual = max(0, int(fresh or 0) - int(remaining or 0))
    rep = speech.report({"chars_actual": actual, "cred": cred, "job_id": job_id,
                         "sale": True})
    return settle(conn, res_id, rep)
