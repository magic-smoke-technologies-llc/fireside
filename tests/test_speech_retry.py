"""A request never goes out over the cap, and a refused voice server is named.

2026-11-17: one chapter of one book failed twice, minutes apart, with
`URLError: <urlopen error [Errno 111] Connection refused>` on a Tower Kokoro
that answers /health fine. Two suspects, both cheap to close: a paragraph with
no sentence break could leave chunk_text with one request bigger than the cap,
and the retry window for a connection refused was 1.5s+3s, which is shorter
than a server restart.
"""
import unittest

from host import jobs
from providers._text import chunk_text


class ChunkCapTest(unittest.TestCase):
    def test_a_wall_of_text_with_no_sentence_break_is_cut(self):
        wall = "word " * 4000          # 20k chars, not one full stop
        parts = chunk_text(wall)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(p) <= 3500 for p in parts),
                        [len(p) for p in parts])

    def test_ordinary_prose_still_splits_on_sentences(self):
        text = ("A short sentence. " * 40) + "\n\n" + ("Another one. " * 40)
        parts = chunk_text(text)
        self.assertTrue(all(len(p) <= 3500 for p in parts))
        self.assertNotIn("Another one. A short", parts[0])

    def test_positive_control_a_small_chapter_is_one_request(self):
        self.assertEqual(chunk_text("Fire burns here. It burns still."),
                         ["Fire burns here. It burns still."])


class _Speech:
    id = "openai"

    def __init__(self, failures):
        self.failures = failures
        self.calls = 0

    def synth(self, text, voice, opts, cred):
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return "audio"


def _refused():
    try:
        raise ConnectionRefusedError(111, "Connection refused")
    except ConnectionRefusedError as e:
        return e


class VoiceServerTest(unittest.TestCase):
    def test_refused_gets_a_longer_window_and_a_readable_message(self):
        slept = []
        sp = _Speech([_refused(), _refused(), _refused(), _refused()])
        with self.assertRaises(jobs.VoiceServerUnavailable) as cm:
            jobs._synth(sp, "hi", "v", {}, {}, sleep=slept.append)
        msg = str(cm.exception).lower()
        self.assertIn("refused the connection", msg)
        self.assertIn("convert again", msg)
        # real seconds, not the 1.5s general backoff
        self.assertEqual(slept, [3.0, 8.0, 15.0])

    def test_a_restart_that_recovers_on_the_second_knock_succeeds(self):
        slept = []
        sp = _Speech([_refused()])
        out = jobs._synth(sp, "hi", "v", {}, {}, sleep=slept.append)
        self.assertEqual(out, "audio")
        self.assertEqual(slept, [3.0])

    def test_a_bad_request_still_fails_fast_with_its_own_error(self):
        slept = []
        sp = _Speech([ValueError("input contains no speakable text")] * 4)
        with self.assertRaises(ValueError):
            jobs._synth(sp, "hi", "v", {}, {}, sleep=slept.append)
        self.assertEqual(slept, [1.5, 3.0, 6.0])


if __name__ == "__main__":
    unittest.main()