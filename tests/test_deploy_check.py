"""A deploy is not done until the live box says the same thing as the deploy.

Twice the unit tests were green and the live server disagreed: the db lock fix
that shipped behind a flag, and #161, where hosted was merged, deployed, and
still false because the container set an environment variable nothing read.

`tools/check_deploy.py` is the gate: it reads /health, waits for the build name
the deploy carried, then compares each behaviour flag. These tests cover the
comparison itself (a wrong switch must fail), and one run against a real server
booted here with a real environment, because the point of the tool is that it
checks the thing, not a description of the thing.
"""
import importlib.util
import json
import os
import pathlib
import tempfile
import threading
from http.server import ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _checker():
    spec = importlib.util.spec_from_file_location("fs_check_deploy", ROOT / "tools" / "check_deploy.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_a_matching_live_box_passes():
    c = _checker()
    live = {"ok": True, "hosted": True, "require_auth": True, "db_backend": "mysql",
            "storage_backend": "azure", "payments_configured": False,
            "build": "sha-abc1234", "readers": [{"id": "epub"}, {"id": "pdf"}]}
    rows = c.compare(live, {"hosted": "true", "payments_configured": "false"},
                     "sha-abc1234", ("pdf",))
    assert all(r[3] for r in rows), rows


def test_a_switch_left_off_fails():
    """The #161 shape: the build is right, hosted is false."""
    c = _checker()
    live = {"hosted": False, "build": "sha-abc1234"}
    rows = c.compare(live, {"hosted": "true"}, "sha-abc1234")
    failed = [r for r in rows if not r[3]]
    assert [r[0] for r in failed] == ["hosted"], rows


def test_false_is_read_as_false_not_as_a_word():
    """payments_configured=false must not pass when the box reports true."""
    c = _checker()
    rows = c.compare({"payments_configured": True}, {"payments_configured": "false"})
    assert rows[0][3] is False, rows


def test_a_field_that_vanished_fails():
    c = _checker()
    rows = c.compare({"hosted": True}, {"sale_connection": "false"})
    assert rows[0][3] is False and rows[0][2] == "<missing>", rows


def test_a_reader_that_did_not_load_fails():
    c = _checker()
    rows = c.compare({"readers": [{"id": "epub"}]}, {}, None, ("pdf",))
    assert rows[0][3] is False, rows


def test_it_waits_for_the_build_then_stops_when_the_switches_disagree():
    """Right revision plus wrong switches must not wait out the timeout."""
    c = _checker()
    calls = []
    ticks = []

    def fetch(_):
        calls.append(1)
        if len(calls) == 1:
            return {"build": "sha-old"}
        return {"build": "sha-new", "hosted": False}

    def sleep(n):
        ticks.append(n)

    rows = c.check("http://x", {"hosted": "true"}, "sha-new",
                   timeout=300, interval=10, fetch=fetch, sleep=sleep, out=lambda *_: None)
    assert len(calls) == 2, calls
    assert len(ticks) == 1, ticks
    assert [r[0] for r in rows if not r[3]] == ["hosted"], rows


def _boot(env):
    from host.app import App, make_handler
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    tmp = tempfile.mkdtemp()
    app = App(os.path.join(tmp, "plugins"), db_path=os.path.join(tmp, "db.sqlite"),
              storage_id="local", hosted=bool(env.get("FIRESIDE_HOSTED")),
              require_auth=True)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, old


def test_the_gate_runs_against_a_real_server():
    """Boot the host the way the container does and check it over HTTP."""
    c = _checker()
    httpd, old = _boot({"FIRESIDE_HOSTED": "1", "FIRESIDE_BUILD": "sha-bEEf123"})
    try:
        base = "http://127.0.0.1:%d" % httpd.server_address[1]
        rows = c.check(base, {"hosted": "true", "require_auth": "true",
                              "build": "sha-bEEf123"},
                       "sha-bEEf123", timeout=5, interval=1, out=lambda *_: None)
        assert all(r[3] for r in rows), rows
        # the same server with the build name we did not deploy must fail
        rows = c.check(base, {}, "sha-other", timeout=0, out=lambda *_: None)
        assert not all(r[3] for r in rows), rows
    finally:
        httpd.shutdown()
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
