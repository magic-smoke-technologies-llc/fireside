"""TOTP against the published vectors, plus the edges that bite in production.

The RFC numbers are the whole point: a hand-rolled HMAC that agrees with my
own idea of correct is worthless. RFC 6238 Appendix B pins the algorithm and
RFC 4226 Appendix D pins HOTP.
"""

import time
import unittest

from host import totp

# RFC 6238 test secret, ASCII "12345678901234567890".
RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"


class HotpVectors(unittest.TestCase):
    """RFC 4226 Appendix D, 6 digits."""

    CASES = [("755224", 0), ("287082", 1), ("359152", 2),
             ("969429", 3), ("338314", 4), ("254676", 5)]

    def test_counters(self):
        for want, counter in self.CASES:
            self.assertEqual(totp.hotp(RFC_SECRET, counter), want)


class TotpVectors(unittest.TestCase):
    """RFC 6238 Appendix B, 8 digits, SHA1 column."""

    CASES = [(59, "94287082"), (1111111109, "07081804"),
             (1111111111, "14050471"), (1234567890, "89005924"),
             (2000000000, "69279037"), (20000000000, "65353130")]

    def test_times(self):
        for at, want in self.CASES:
            self.assertEqual(totp.totp(RFC_SECRET, at=at, digits=8), want)


class Verify(unittest.TestCase):
    def test_current_step_matches(self):
        secret = totp.new_secret()
        now = time.time()
        self.assertIsNotNone(totp.verify(secret, totp.totp(secret, at=now), at=now))

    def test_previous_step_still_matches_inside_window(self):
        secret = totp.new_secret()
        now = time.time()
        self.assertIsNotNone(
            totp.verify(secret, totp.totp(secret, at=now - totp.STEP), at=now))

    def test_two_steps_back_is_refused(self):
        secret = totp.new_secret()
        now = time.time()
        self.assertIsNone(
            totp.verify(secret, totp.totp(secret, at=now - 3 * totp.STEP), at=now))

    def test_wrong_code_is_refused(self):
        secret = totp.new_secret()
        now = time.time()
        good = totp.totp(secret, at=now)
        bad = "%06d" % ((int(good) + 1) % 1000000)
        self.assertIsNone(totp.verify(secret, bad, at=now))

    def test_another_secret_is_refused(self):
        a, b = totp.new_secret(), totp.new_secret()
        now = time.time()
        self.assertIsNone(totp.verify(a, totp.totp(b, at=now), at=now))

    def test_spaces_and_junk_are_tolerated(self):
        secret = totp.new_secret()
        now = time.time()
        code = totp.totp(secret, at=now)
        spaced = " " + code[:3] + " " + code[3:] + "\n"
        self.assertIsNotNone(totp.verify(secret, spaced, at=now))

    def test_empty_and_short_input_does_not_raise(self):
        secret = totp.new_secret()
        for junk in ("", None, "12345", "abcdef", 12345):
            self.assertIsNone(totp.verify(secret, junk), junk)

    def test_garbage_secret_is_refused_not_raised(self):
        self.assertIsNone(totp.verify("not base32!!", "123456"))
        self.assertEqual(totp.totp("not base32!!"), "")

    def test_secret_with_padding_and_lowercase_would_still_work(self):
        secret = totp.new_secret()
        now = time.time()
        lowered = secret.lower() + "="
        self.assertIsNotNone(totp.verify(lowered, totp.totp(secret, at=now), at=now))


class Secrets(unittest.TestCase):
    def test_two_secrets_differ_and_are_app_friendly(self):
        a, b = totp.new_secret(), totp.new_secret()
        self.assertNotEqual(a, b)
        self.assertEqual(len(a), 32)
        self.assertNotIn("=", a)
        self.assertEqual(a, a.upper())

    def test_uri_is_scannable(self):
        secret = totp.new_secret()
        uri = totp.provisioning_uri(secret, "chris")
        self.assertTrue(uri.startswith("otpauth://totp/"))
        self.assertIn("secret=" + secret, uri)
        self.assertIn("issuer=Fireside", uri)
        self.assertIn("digits=6", uri)
        self.assertIn("period=30", uri)

    def test_account_with_specials_is_escaped(self):
        uri = totp.provisioning_uri(totp.new_secret(), "chris@example.com")
        self.assertIn("chris%40example.com", uri)


class RecoveryCodes(unittest.TestCase):
    def test_shape_and_uniqueness(self):
        codes = totp.new_recovery_codes()
        self.assertEqual(len(codes), totp.RECOVERY_CODES)
        self.assertEqual(len(set(codes)), len(codes))
        for c in codes:
            self.assertRegex(c, r"^[2-9a-hj-km-np-z]{5}-[2-9a-hj-km-np-z]{5}$")


if __name__ == "__main__":
    unittest.main()
