"""Token bucket: the gate before our key runs."""

import unittest

from host.ratelimit import RateLimiter


class TestRateLimiter(unittest.TestCase):
    def test_burst_then_deny(self):
        rl = RateLimiter(rate_per_min=60, burst=3)
        self.assertTrue(rl.allow("u", now=0))
        self.assertTrue(rl.allow("u", now=0))
        self.assertTrue(rl.allow("u", now=0))
        self.assertFalse(rl.allow("u", now=0))

    def test_refills_over_time(self):
        rl = RateLimiter(rate_per_min=60, burst=1)     # 1 per second
        self.assertTrue(rl.allow("u", now=0))
        self.assertFalse(rl.allow("u", now=0.5))
        self.assertTrue(rl.allow("u", now=1.1))

    def test_per_key(self):
        rl = RateLimiter(rate_per_min=60, burst=1)
        self.assertTrue(rl.allow("a", now=0))
        self.assertTrue(rl.allow("b", now=0), "other users are unaffected")

    def test_idle_keys_are_forgotten_but_busy_ones_are_not(self):
        from host import ratelimit
        old = ratelimit.PRUNE_ABOVE
        ratelimit.PRUNE_ABOVE = 10
        self.addCleanup(lambda: setattr(ratelimit, "PRUNE_ABOVE", old))
        rl = RateLimiter(rate_per_min=60, burst=2)      # refills in 2s
        for i in range(30):
            rl.allow("k%d" % i, now=0)
        self.assertTrue(rl.allow("busy", now=100))
        self.assertTrue(rl.allow("busy", now=100))
        self.assertFalse(rl.allow("busy", now=100))     # drained, so kept
        rl.allow("trigger", now=100.5)                  # over the limit: prune
        self.assertLess(len(rl._state), 5)
        self.assertFalse(rl.allow("busy", now=100.5), "a drained bucket survives")

    def test_concurrent_callers_cannot_overspend_the_last_token(self):
        import threading
        rl = RateLimiter(rate_per_min=0.0001, burst=5)
        passed = []
        def hit():
            passed.append(rl.allow("same", now=0))
        ts = [threading.Thread(target=hit) for _ in range(50)]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual(sum(passed), 5)


if __name__ == "__main__":
    unittest.main()
