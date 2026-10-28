"""Fixtures for suites that are not about the second factor.

An administrator account always carries a factor, so any fixture that signs in
as the first account now owes one - exactly like a human on a fresh box. These
helpers do what that human does: enrol once, then finish each later login with a
fresh code.

A code must not be reused: the replay guard refuses a counter it has already
seen, so a login after enrolment asks for the NEXT step, not this one.
"""
import time

from host import totp

SECRETS = {}


def enrol(post, name):
    """Enrol the signed-in account and promote the session it is on.

    `post(path, obj)` is the caller's own JSON post helper. Returns the secret.
    """
    info = post("/2fa/enroll", {})
    post("/2fa/confirm", {"code": totp.totp(info["secret"], at=time.time())})
    SECRETS[name] = info["secret"]
    return info["secret"]


def finish(post, name):
    """Finish a login that is waiting for a code."""
    post("/2fa/verify",
         {"code": totp.totp(SECRETS[name], at=time.time() + totp.STEP)})


def signup(post, name):
    """Register the first account (an admin) and clear its factor.

    Returns whatever the register route returned.
    """
    out = post("/auth/local/register",
               {"username": name, "password": "longenough"})
    if isinstance(out, dict) and out.get("admin"):
        enrol(post, name)
    return out
