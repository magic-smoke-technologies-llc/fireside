"""One real host process, one real browser, throwaway data.

Why a subprocess and not an in-process App: the thing under test is the page a
deployment actually serves. `python3 -m host.app` is the same entrypoint the
container runs, so the suite inherits the real defaults (plugins discovered from
./plugins, local storage, the auth gate that is ON unless the operator passes
--open) instead of a test-shaped approximation of them.

Why the temp dir and the env scrub: every FIRESIDE_* variable from the shell is
dropped before the child starts. A developer's own Azure key, MySQL DSN or
payment provider must not be able to sit in the environment and change what the
suite is proving. The proof is only worth the isolation behind it.

Nothing here reaches the network. Local speech and local storage are the
defaults, and the example book ships in the repo.
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARTIFACTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_artifacts")
PLUGINS = os.path.join(REPO, "plugins")


def _free_port():
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


class LocalHost:
    """A host process on a free port, over a data dir that is thrown away."""

    def __init__(self, plugins=None):
        self.data = tempfile.mkdtemp(prefix="fireside-ui-")
        self.plugins = plugins or PLUGINS
        self.port = _free_port()
        self.proc = None
        self._output = ""
        self.start()

    # -- lifecycle ------------------------------------------------------------

    def start(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("FIRESIDE_")}
        env.update({
            "FIRESIDE_DATA": self.data,
            "FIRESIDE_PLUGINS_ROOT": self.plugins,
            "PYTHONPATH": REPO,
            "PYTHONUNBUFFERED": "1",
        })
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "host.app",
             "--host", "127.0.0.1", "--port", str(self.port),
             "--data", self.data, "--plugins", self.plugins],
            cwd=REPO, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self._wait_health()

    def _wait_health(self, timeout=90):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("host exited before it answered /health\n"
                                   + self.drain())
            try:
                with urllib.request.urlopen(self.url("/health"), timeout=1) as r:
                    if r.status == 200:
                        return
            except Exception:
                pass
            time.sleep(0.1)
        raise RuntimeError("host never answered /health on port %d\n%s"
                           % (self.port, self.drain()))

    def drain(self):
        """Everything the child has printed so far (only safe once it is dead)."""
        if self.proc and self.proc.stdout and self.proc.poll() is not None:
            try:
                self._output += self.proc.stdout.read() or ""
            except Exception:
                pass
        return self._output

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
        try:
            import shutil
            shutil.rmtree(self.data, ignore_errors=True)
        except Exception:
            pass

    # -- talking to it --------------------------------------------------------

    def url(self, path="/"):
        return "http://127.0.0.1:%d%s" % (self.port, path)

    def request(self, path, cookies=None, method="GET", body=None):
        """(status, parsed json) with an optional cookie jar. Never raises on 4xx."""
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if cookies:
            headers["Cookie"] = "; ".join("%s=%s" % (k, v) for k, v in cookies.items())
        req = urllib.request.Request(self.url(path), data=data, headers=headers,
                                     method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, (json.loads(raw) if raw else None)
            except ValueError:
                return e.code, None


def wait_hidden(page, selector, timeout=20000):
    page.wait_for_selector(selector, state="hidden", timeout=timeout)


def wait_visible(page, selector, timeout=20000):
    page.wait_for_selector(selector, state="visible", timeout=timeout)


def wait_until(page, expression, timeout=20000):
    """Poll an expression in the page. Survives the reloads the sign-in door does,
    because wait_for_function re-evaluates after a navigation instead of dying
    with the old document."""
    page.wait_for_function(expression, timeout=timeout)


def cookie_jar(context):
    return {c["name"]: c["value"] for c in context.cookies()}


def system_chromium():
    """A Chromium that already lives on this box, if one does.

    CI runs `playwright install chromium`, so the browser it manages is used
    first. On a machine that already ships chromium (a dev box, this sandbox)
    the managed cache may not exist at all, and telling someone to download a
    second browser to run the tests is a good way to have no tests.
    """
    for candidate in ("/usr/bin/chromium", "/usr/bin/chromium-browser",
                      "/usr/bin/google-chrome"):
        if os.path.exists(candidate):
            return candidate
    return None


class BrowserTest(unittest.TestCase):
    """A fresh host and a fresh browser context per test: the isolation a
    stranger's first visit has, which is the thing being tested."""

    browser = None
    playwright = None

    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:                                  # pragma: no cover
            raise unittest.SkipTest(
                "playwright is not installed: pip install playwright && "
                "python -m playwright install chromium")
        cls.playwright = sync_playwright().start()
        launch = {"args": ["--no-sandbox", "--disable-dev-shm-usage"]}
        # explicit override, else the browser playwright manages, else the system one
        exe = os.environ.get("FIRESIDE_UI_BROWSER")
        if exe and os.path.exists(exe):
            launch["executable_path"] = exe
        else:
            managed = cls.playwright.chromium.executable_path
            if not (managed and os.path.exists(managed)):
                fallback = system_chromium()
                if fallback:
                    launch["executable_path"] = fallback
        cls.browser = cls.playwright.chromium.launch(**launch)

    @classmethod
    def tearDownClass(cls):
        if cls.browser:
            cls.browser.close()
        if cls.playwright:
            cls.playwright.stop()

    def setUp(self):
        self.host = LocalHost()
        self.context = self.browser.new_context(viewport={"width": 1280, "height": 900})
        self.page = self.context.new_page()
        self.page_errors = []          # uncaught exceptions: always a defect
        self.console_errors = []       # console.error: sometimes the expected refusal
        self.page.on("pageerror", lambda e: self.page_errors.append(str(e)))
        self.page.on("console", self._on_console)

    def tearDown(self):
        # A screenshot on failure is the difference between a red CI run and a
        # five-minute mystery. CI uploads tests_ui/_artifacts when the job fails.
        failed = not getattr(self._outcome, "success", True)
        if failed:
            try:
                os.makedirs(ARTIFACTS, exist_ok=True)
                name = "%s-%s.png" % (type(self).__name__, self._testMethodName)
                self.page.screenshot(path=os.path.join(ARTIFACTS, name), full_page=True)
            except Exception:
                pass
        try:
            self.context.close()
        finally:
            self.host.stop()

    def _on_console(self, message):
        if message.type != "error" or "favicon" in message.text:
            return
        self.console_errors.append(message.text)

    def open_site(self):
        self.page.goto(self.host.url("/"))
        wait_until(self.page, "() => document.readyState === 'complete'")
        return self.page

    def register(self, username, password="bench-correction-1", email=None):
        """Create an account through the door itself, not through the API."""
        self.open_site()
        wait_visible(self.page, "#lock")
        wait_until(self.page, "() => document.querySelector('#locktitle')"
                               ".textContent.trim().length > 0")
        if self.page.text_content("#locktitle").strip() != "set up fireside":
            # an account already exists on this host: take the create toggle
            self.page.click("#locktoggle")
        self.page.fill("#lockuser", username)
        self.page.fill("#lockemail", email or "%s@example.test" % username)
        self.page.fill("#lockpass", password)
        self.page.fill("#lockpass2", password)
        self.page.click("#lockgo")
        self.wait_in_app()
        return username

    def sign_in(self, username, password="bench-correction-1"):
        self.open_site()
        wait_visible(self.page, "#lock")
        self.page.fill("#lockuser", username)
        self.page.fill("#lockpass", password)
        self.page.click("#lockgo")
        self.wait_in_app()

    def sign_out(self):
        self.page.click("#avatar")
        wait_visible(self.page, "#usermenu")
        self.page.click("#signout")
        wait_visible(self.page, "#lock")

    def wait_in_app(self):
        """The door closed and the library view is the one that is showing."""
        wait_until(self.page, "() => document.body.classList.contains('locked') === false"
                              " && !!document.querySelector('#v-lib.on')")

    def shelf(self):
        """What the shelf actually renders: title + the voiced line, per cell."""
        return self.page.evaluate("""() => Array.from(document.querySelectorAll('#shelf .bk')).map(
            el => ({ title: (el.children[1] || {}).textContent || '',
                     line: (el.children[3] || {}).textContent || '' }))""")

    def library_via_host(self):
        """The same user's shelf, asked of the host directly with the browser's
        own cookies. If the DOM and this disagree, the UI is lying about the
        user's books and the test should say so."""
        status, body = self.host.request("/library", cookies=cookie_jar(self.context))
        self.assertEqual(status, 200, "the host refused the browser's own session")
        return body or []

    def assert_no_page_errors(self, allow_console=()):
        """No uncaught exception, and no console error outside what the test
        deliberately provoked. The page installs its own window error handler
        that pulls the sign-in door back over the app, so an uncaught throw
        during boot shows up as a suspiciously empty screen, not a crash."""
        self.assertEqual(self.page_errors, [], "the page threw while we were testing it")
        allowed = tuple(allow_console)
        unexpected = [e for e in self.console_errors if not any(a in e for a in allowed)]
        self.assertEqual(unexpected, [], "the browser logged errors nobody asked for")

    def wait_library(self, count):
        """Wait for the shelf to have finished loading. #libempty is present from
        the start, so its inline display is the signal that loadLibrary ran:
        it stays '' until the host answers."""
        try:
            if count:
                wait_until(self.page, "() => document.querySelectorAll('#shelf .bk').length === %d" % count)
            else:
                wait_until(self.page, "() => document.querySelector('#libempty').style.display === 'block'"
                                      " && document.querySelectorAll('#shelf .bk').length === 0")
        except Exception:
            # a bare TimeoutError says nothing about what went wrong. Say it.
            seen = self.shelf()
            raise AssertionError(
                "shelf never settled to %d book(s); it showed %d: %r"
                % (count, len(seen), [c["title"] for c in seen]))
