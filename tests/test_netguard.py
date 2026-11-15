"""Outbound fetches stay off private networks, directly and through redirects."""

import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from host import egress, netguard


class Redirector(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/ok":
            body = b"<p>hello</p>"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(302)
            self.send_header("Location", self.server.target)
            self.end_headers()

    def log_message(self, *a):
        pass


class TestCheckPublic(unittest.TestCase):
    def test_private_forms_are_refused(self):
        for url in ("http://127.0.0.1/x", "http://localhost/x", "http://[::1]/x",
                    "http://169.254.169.254/latest/meta-data", "http://10.0.0.5/",
                    "http://192.168.1.1/", "http://100.64.0.1/",
                    "http://[::ffff:127.0.0.1]/", "http://0.0.0.0/"):
            with self.assertRaises(netguard.BlockedAddress, msg=url):
                netguard.check_public(url)

    def test_non_http_schemes_are_refused(self):
        for url in ("file:///etc/passwd", "ftp://example.com/x", "gopher://x/", ""):
            with self.assertRaises(netguard.BlockedAddress, msg=url):
                netguard.check_public(url)

    def test_a_public_address_passes(self):
        netguard.check_public("http://93.184.216.34/x")

    def test_the_operator_opt_out_lets_a_private_address_through(self):
        with mock.patch.dict(os.environ, {"FIRESIDE_ALLOW_PRIVATE_FETCH": "1"}):
            netguard.check_public("http://127.0.0.1/x")


class TestFetchGuard(unittest.TestCase):
    def setUp(self):
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), Redirector)
        self.srv.target = ""
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.shutdown)

    def test_fetch_refuses_a_private_url_without_retrying(self):
        calls = []
        real = netguard.check_public
        with mock.patch.object(netguard, "check_public",
                               side_effect=lambda u: (calls.append(u), real(u))[1]):
            with self.assertRaises(netguard.BlockedAddress):
                egress.fetch("http://127.0.0.1:%d/ok" % self.port,
                             transport={"retries": 3, "backoff": 0})
        self.assertEqual(len(calls), 1)

    def test_a_redirect_into_a_private_address_is_refused(self):
        # The first hop is allowed (opt-out for the test server); the hop to the
        # metadata address is checked on its own and must be refused.
        self.srv.target = "http://169.254.169.254/latest/meta-data"
        real = netguard.check_public

        def only_the_target(url):
            if url.startswith("http://127.0.0.1"):
                return                       # the test server itself
            real(url)

        with mock.patch.object(netguard, "check_public", side_effect=only_the_target):
            with self.assertRaises(netguard.BlockedAddress):
                egress.fetch("http://127.0.0.1:%d/hop" % self.port,
                             transport={"retries": 3, "backoff": 0})

    def test_a_public_fetch_with_the_opt_out_still_works(self):
        with mock.patch.dict(os.environ, {"FIRESIDE_ALLOW_PRIVATE_FETCH": "1"}):
            self.srv.target = "http://127.0.0.1:%d/ok" % self.port
            text = egress.fetch("http://127.0.0.1:%d/hop" % self.port)
        self.assertIn("hello", text)


if __name__ == "__main__":
    unittest.main()
