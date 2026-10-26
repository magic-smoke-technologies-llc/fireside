#!/usr/bin/env python3
"""Clear one account's second factor, from inside the deployment.

The only thing this exists for: an administrator lost the phone AND the recovery
codes. Administrators always carry a factor here, so there is no button that
turns it off. The honest out is out of band, and the container is out of band:
reaching this command means holding the deploy credentials.

It does NOT leave the account with one key. It deletes the secret and every
recovery code, revokes that account's sessions, and the next login is forced to
enrol again. So the rule survives the rescue.

    python3 tools/reset_2fa.py --user chris --confirm chris

Run it with the same FIRESIDE_DB_* environment the host runs with, or point at
a SQLite file with --db.
"""
import argparse
import sys

import host.auth as auth
import host.db as db


def _find(conn, who):
    rows = conn.execute("SELECT user_id,username FROM local_accounts").fetchall()
    for r in rows:
        if r["user_id"] == who or (r["username"] or "").lower() == who.lower():
            return r["user_id"], r["username"]
    return None, None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--user", required=True,
                    help="username or user_id whose factor should be cleared")
    ap.add_argument("--db", default=None,
                    help="sqlite file; otherwise FIRESIDE_DB_CONN is used")
    ap.add_argument("--confirm", required=True, metavar="USER",
                    help="repeat the username being cleared (a typo must not "
                         "rescue the wrong account)")
    a = ap.parse_args(argv)

    if a.confirm.strip().lower() != a.user.strip().lower():
        print("refusing: --confirm %r does not match --user %r"
              % (a.confirm, a.user), file=sys.stderr)
        return 2

    conn = db.connect(a.db)
    uid, name = _find(conn, a.user)
    if uid is None:
        print("no such account: %s" % a.user, file=sys.stderr)
        return 1

    was_enrolled = auth.enrolled(conn, uid)
    was_admin = auth.is_admin(conn, uid)
    auth.disable_2fa(conn, uid)
    revoked = auth.revoke_sessions(conn, uid)

    print("cleared %s (%s)%s; %d session(s) revoked"
          % (name, uid, " [admin]" if was_admin else "", revoked))
    print("factor was %s" % ("on" if was_enrolled else "not set"))
    print("next login for %s must enrol a new authenticator before it opens "
          "anything" % name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
