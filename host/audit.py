"""Who did what to someone else's account, kept as evidence rather than inferred.

The ledger answers "what is this balance" and a charge row is enough for that.
It is not an audit trail: its note is prose written for a reader, it does not
say whether an action was refused, and it records the amount but not the
balance on either side in a shape anything can compare.

So every staff action that moves money or authority writes one row here, before
the caller hears back and whether it was applied or refused. Rows are only ever
inserted. Reading is an admin route, and the screen that shows it is the one an
operator looks at when a balance moves and nobody remembers why.
"""

import time
import uuid

from host.db import now

MAX = 500     # how far back the screen reads, not how much is kept


def record(conn, actor_id, actor_name, action, target="", amount=0.0,
           before=0.0, after=0.0, outcome="applied", reason="", note="", ip=""):
    """Write one audit line. Returns its id.

    `outcome` is applied or refused: a refused attempt is a fact about the
    staff, not about the balance, and it is the row an operator wants when a
    balance did NOT move and someone says they changed it.
    """
    # Microseconds first, so two rows made in the same second still read in the
    # order they happened. created_at has second resolution and is the column a
    # person reads, which makes the id the tie-breaker that is not random.
    rid = "%020d-%s" % (int(time.time() * 1000000), uuid.uuid4().hex[:8])
    conn.execute(
        "INSERT INTO admin_audit(id,actor_id,actor_name,action,target,amount,"
        "balance_before,balance_after,outcome,reason,note,created_at,ip)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rid, actor_id or "", actor_name or "", action or "", target or "",
         float(amount or 0.0), float(before or 0.0), float(after or 0.0),
         outcome or "applied", (reason or "")[:255], note or "", now(), ip or ""))
    conn.commit()
    return rid


def recent(conn, limit=20):
    rows = conn.execute(
        "SELECT * FROM admin_audit ORDER BY created_at DESC, id DESC LIMIT ?",
        (int(limit),))
    return [dict(r) for r in rows]