#!/usr/bin/env python3
"""Refuse to call a deploy done until the live box agrees with the config.

Three times now a merged, deployed, CI-green change has been switched off in
production: the fix shipped and the live flag still said otherwise (#161: the
container set an env var, nothing read it, /health said hosted:false). Tests
cannot catch that, because tests run the code, not the container.

So the deploy carries its name. FIRESIDE_BUILD is set to the image tag being
deployed, /health reports it, and this tool waits for that name to show up and
then checks every behaviour flag against what the deploy intended.

    python3 tools/check_deploy.py --base-url https://readfireside.com \
        --expect-build sha-7929b99 \
        --expect hosted=true --expect require_auth=true \
        --expect storage_backend=azure --expect db_backend=mysql \
        --expect payments_configured=false \
        --need-reader epub --need-reader pdf

Exit 0 only when every expected fact matched within the timeout; anything else
is exit 1 with the live value printed next to the expected one.
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request

# Keys whose live value is a bool. Checked as booleans, so "false" really means
# false rather than the truthy string "false".
BOOL_KEYS = {"hosted", "require_auth", "payments_configured", "sale_connection", "ok"}


class Unreachable(Exception):
    pass


def fetch_health(base_url, timeout=15):
    url = base_url.rstrip("/") + "/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode() or "{}")
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise Unreachable("%s: %s" % (url, e))


def _as_bool(v):
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def compare(live, expect, expect_build=None, need_readers=()):
    """Return a list of (label, expected, actual, ok). Pure, so it can be tested.

    Every expected fact is compared. A missing key is a failure, not a pass: a
    field that vanished from /health is exactly the shape of the bug this tool
    exists for.
    """
    rows = []
    if expect_build is not None:
        actual = live.get("build")
        rows.append(("build", expect_build, actual, str(actual or "") == expect_build))
    for key, want in expect.items():
        if key not in live:
            rows.append((key, want, "<missing>", False))
            continue
        actual = live.get(key)
        if key in BOOL_KEYS:
            ok = _as_bool(actual) == _as_bool(want)
        else:
            ok = str(actual) == str(want)
        rows.append((key, want, actual, ok))
    have = {r.get("id") for r in (live.get("readers") or []) if isinstance(r, dict)}
    for rid in need_readers:
        rows.append(("reader:" + rid, "loaded", "loaded" if rid in have else "absent",
                     rid in have))
    return rows


def check(base_url, expect=None, expect_build=None, need_readers=(),
          timeout=300, interval=10, fetch=fetch_health, sleep=time.sleep,
          out=print):
    """Poll until the live box matches, then report. Raises nothing: returns rows.

    Waits for the build name first (a scale-to-zero revision takes time to
    answer at all), then holds every other fact to the same standard.
    """
    expect = dict(expect or {})
    deadline = time.time() + timeout
    rows = []
    while True:
        try:
            live = fetch(base_url)
        except Unreachable as e:
            rows = [("reachable", "yes", str(e), False)]
            if time.time() >= deadline:
                out("DEPLOY NOT VERIFIED: box never answered")
                for r in rows:
                    out("  FAIL  %-22s expected %s  got %s" % r[:3])
                return rows
            out("waiting: %s" % e)
            sleep(interval)
            continue
        rows = compare(live, expect, expect_build, need_readers)
        if all(r[3] for r in rows):
            out("live config matches the deploy")
            for label, want, actual, _ in rows:
                out("  ok    %-22s %s" % (label, actual))
            return rows
        wrong_build = expect_build is not None and not rows[0][3]
        if time.time() >= deadline:
            out("DEPLOY NOT VERIFIED: live config disagrees with the deploy")
            for label, want, actual, ok in rows:
                out("  %s  %-22s expected %s  got %s"
                    % ("ok  " if ok else "FAIL", label, want, actual))
            return rows
        if not wrong_build:
            # Right revision, wrong switches: no amount of waiting fixes that.
            out("DEPLOY NOT VERIFIED: live config disagrees with the deploy")
            for label, want, actual, ok in rows:
                out("  %s  %-22s expected %s  got %s"
                    % ("ok  " if ok else "FAIL", label, want, actual))
            return rows
        out("waiting: live build is %r, want %r" % (live.get("build"), expect_build))
        sleep(interval)


def parse_expect(pairs):
    out = {}
    for p in pairs or ():
        if "=" not in p:
            raise SystemExit("--expect wants key=value, got %r" % p)
        k, v = p.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--expect-build", default=None,
                    help="the FIRESIDE_BUILD value this deploy was supposed to serve")
    ap.add_argument("--expect", action="append", default=[],
                    help="key=value fact from /health, repeatable")
    ap.add_argument("--need-reader", action="append", default=[],
                    help="reader plugin id that must be loaded, repeatable")
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--interval", type=int, default=10)
    args = ap.parse_args(argv)
    rows = check(args.base_url, parse_expect(args.expect), args.expect_build,
                 tuple(args.need_reader), timeout=args.timeout, interval=args.interval)
    return 0 if all(r[3] for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
