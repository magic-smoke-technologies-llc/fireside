"""Pronunciation fixes: store, apply, cache tag, and render wiring."""

import os
import tempfile
import unittest

from host import connections, jobs, library, pron
from host.contracts import Audio, Book, Chapter, Quote, Report
from host.db import connect
from storage.local import LocalBackend


class FakeSpeech:
    id = "fake"
    ext = "mp3"
    mime = "audio/mpeg"
    needs_key = False

    def __init__(self):
        self.calls = 0
        self.texts = []

    def synth(self, text, voice, opts, cred):
        self.calls += 1
        self.texts.append(text)
        return Audio(data=text.encode(), ext="mp3", mime="audio/mpeg")

    def list_voices(self, cred):
        return []

    def quote(self, ctx):
        return Quote()

    def report(self, ctx):
        return Report(charge="charged")


def _book(texts=("Qui-Gon spoke.", "nothing here.", "Qui-Gon again.")):
    return Book(title="B", chapters=[Chapter("c%d" % i, t) for i, t in enumerate(texts)])


class TestPron(unittest.TestCase):
    def setUp(self):
        self.conn = connect(":memory:")

    def test_save_load_roundtrip_and_tidy(self):
        rules = [{"from": "Qui-Gon", "to": "Kwai-Gon"},
                 {"from": "  ", "to": "x"},          # blank dropped
                 {"from": "Qui-Gon", "to": "dup"},   # duplicate dropped
                 {"from": "Jinn", "to": "Jin"}]
        out = pron.save(self.conn, "u1", rules)
        self.assertEqual([r["from"] for r in out], ["Qui-Gon", "Jinn"])
        self.assertEqual(pron.load(self.conn, "u1"), out)
        self.assertEqual(pron.load(self.conn, "nobody"), [])

    def test_apply_whole_word_case_insensitive(self):
        rules = [{"from": "Qui-Gon", "to": "Kwai-Gon"}]
        self.assertEqual(pron.apply("Qui-Gon Jinn met qui-gon.", rules),
                         "Kwai-Gon Jinn met Kwai-Gon.")
        self.assertEqual(pron.apply("aQui-Gonb", rules), "aQui-Gonb")

    def test_token_for_only_matching_rules(self):
        rules = [{"from": "Qui-Gon", "to": "Kwai-Gon"},
                 {"from": "Hermione", "to": "Her-my-oh-nee"}]
        t1 = pron.token_for("Qui-Gon spoke.", rules)
        self.assertTrue(t1)
        self.assertEqual(pron.token_for("nothing here", rules), "")
        # a chapter that only has Qui-Gon is unaffected by the Hermione rule
        self.assertEqual(t1, pron.token_for("Qui-Gon spoke.", [rules[0]]))

    def test_prefs_and_pron_do_not_clobber(self):
        pron.save(self.conn, "u2", [{"from": "A", "to": "B"}])
        connections.set_prefs(self.conn, "u2", voice="v")
        self.assertEqual(pron.load(self.conn, "u2"),
                         [{"from": "A", "to": "B", "book": "", "author": "",
                           "series": ""}])
        self.assertEqual(connections.get_prefs(self.conn, "u2")["default_voice"], "v")
        connections.set_prefs(self.conn, "u2", connection_id="c1")
        self.assertEqual(pron.load(self.conn, "u2"),
                         [{"from": "A", "to": "B", "book": "", "author": "",
                           "series": ""}])


class TestRenderAppliesPron(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fireside-pron-")
        self.storage = LocalBackend(self.tmp + "/blobs")
        self.conn = connect(":memory:")
        library.create_user(self.conn, "u1")
        self.bid = library.add_book(self.conn, self.storage, "u1", _book())
        self.speech = FakeSpeech()

    def test_rule_applied_and_only_matching_chapters_reencode(self):
        pron.save(self.conn, "u1", [{"from": "Qui-Gon", "to": "Kwai-Gon"}])
        jobs.render_book(self.conn, self.storage, self.speech, "u1", self.bid, voice="v1")
        self.assertIn("Kwai-Gon spoke.", self.speech.texts)
        self.assertEqual(self.speech.calls, 3)
        # same rules again: every chapter is a cache hit, nothing is synthesized
        self.speech.calls = 0
        jobs.render_book(self.conn, self.storage, self.speech, "u1", self.bid, voice="v1")
        self.assertEqual(self.speech.calls, 0, "unchanged pron must hit the cache")
        # add a rule that only touches chapter 1; chapter 0 must not re-synth
        pron.save(self.conn, "u1", [{"from": "Qui-Gon", "to": "Kwai-Gon"},
                                    {"from": "nothing", "to": "nuthin"}])
        self.speech.calls = 0
        jobs.render_book(self.conn, self.storage, self.speech, "u1", self.bid, voice="v1")
        self.assertEqual(self.speech.calls, 1, "only the chapter with the new word re-renders")
        self.assertIn("nuthin here.", self.speech.texts)


class TestPronImpact(unittest.TestCase):
    """Saving a word names the chapters it makes stale, and nothing else."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fireside-pronimpact-")
        self.storage = LocalBackend(self.tmp + "/blobs")
        self.conn = connect(":memory:")
        library.create_user(self.conn, "u1")
        self.bid = library.add_book(self.conn, self.storage, "u1", _book())
        self.speech = FakeSpeech()

    def test_impact_is_empty_until_a_rule_changes_something(self):
        jobs.render_book(self.conn, self.storage, self.speech, "u1", self.bid, voice="v1")
        self.assertEqual(pron.impact(self.conn, self.storage, "u1")["chapters_affected"], 0)

    def test_impact_names_only_the_chapters_holding_the_word(self):
        jobs.render_book(self.conn, self.storage, self.speech, "u1", self.bid, voice="v1")
        # Qui-Gon lives in chapters 0 and 2, never in 1
        pron.save(self.conn, "u1", [{"from": "Qui-Gon", "to": "Kwai-Gon"}])
        im = pron.impact(self.conn, self.storage, "u1")
        self.assertEqual(im["chapters_affected"], 2)
        self.assertEqual(im["books_affected"], 1)
        self.assertEqual(sorted(c["idx"] for c in im["books"][0]["chapters"]), [0, 2])
        rend = im["books"][0]["renditions"][0]
        self.assertEqual(sorted(rend["chapters"]), [0, 2])
        self.assertEqual(rend["voice"], "v1")
        self.assertEqual(im["books"][0]["id"], self.bid)
        # re-encoding exactly those clears the impact
        jobs.render_book(self.conn, self.storage, self.speech, "u1", self.bid,
                         voice="v1", only=[0, 2])
        self.assertEqual(pron.impact(self.conn, self.storage, "u1")["chapters_affected"], 0)

    def test_a_different_voice_is_reported_separately(self):
        jobs.render_book(self.conn, self.storage, self.speech, "u1", self.bid, voice="v1")
        jobs.render_book(self.conn, self.storage, self.speech, "u1", self.bid, voice="v2")
        pron.save(self.conn, "u1", [{"from": "Qui-Gon", "to": "Kwai-Gon"}])
        im = pron.impact(self.conn, self.storage, "u1")
        voices = sorted(r["voice"] for r in im["books"][0]["renditions"])
        self.assertEqual(voices, ["v1", "v2"])
        # the same two chapters are stale in each voice
        self.assertEqual(im["chapters_affected"], 2)

    def test_never_voiced_chapters_are_not_reported(self):
        jobs.render_book(self.conn, self.storage, self.speech, "u1", self.bid,
                         voice="v1", only=[0, 2])
        pron.save(self.conn, "u1", [{"from": "Qui-Gon", "to": "Kwai-Gon"}])
        im = pron.impact(self.conn, self.storage, "u1")
        self.assertEqual(sorted(c["idx"] for c in im["books"][0]["chapters"]), [0, 2])


class ChunkedSpeech:
    """A provider that speaks one paragraph per request, like the real ones do
    every CHUNK_TARGET characters."""
    id = "chunked"
    ext = "mp3"
    mime = "audio/mpeg"
    needs_key = False

    def __init__(self):
        self.calls = 0
        self.texts = []

    def synth(self, text, voice, opts, cred):
        self.calls += 1
        self.texts.append(text)
        return Audio(data=text.encode(), ext="mp3", mime="audio/mpeg")

    def split(self, text):
        return text.split("\n\n")

    def list_voices(self, cred):
        return []


class TestChunkRerender(unittest.TestCase):
    """A word change is the re-render unit: only the chunk holding the word is
    synthesised again, not the whole chapter."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fireside-chunk-")
        self.storage = LocalBackend(self.tmp + "/blobs")
        self.conn = connect(":memory:")
        library.create_user(self.conn, "u1")
        book = Book(title="B", chapters=[
            Chapter("c0", "plain paragraph.\n\nanother plain one."),
            Chapter("c1", "first half.\n\nQui-Gon spoke."),
        ])
        self.bid = library.add_book(self.conn, self.storage, "u1", book)
        self.speech = ChunkedSpeech()

    def test_word_change_reencodes_one_chunk_not_the_chapter(self):
        jobs.render_book(self.conn, self.storage, self.speech, "u1", self.bid, voice="v1")
        self.assertEqual(self.speech.calls, 4)  # 2 chapters x 2 chunks
        self.speech.calls = 0
        self.speech.texts = []
        pron.save(self.conn, "u1", [{"from": "Qui-Gon", "to": "Kwai-Gon"}])
        jobs.render_book(self.conn, self.storage, self.speech, "u1", self.bid, voice="v1")
        self.assertEqual(self.speech.calls, 1, "only the chunk holding the word re-synths")
        self.assertEqual(self.speech.texts, ["Kwai-Gon spoke."])

    def test_impact_reports_new_audio_estimate(self):
        jobs.render_book(self.conn, self.storage, self.speech, "u1", self.bid, voice="v1")
        pron.save(self.conn, "u1", [{"from": "Qui-Gon", "to": "Kwai-Gon"}])
        im = pron.impact(self.conn, self.storage, "u1",
                         split_for=lambda p: self.speech.split)
        self.assertEqual(im["chapters_affected"], 1)
        self.assertEqual(im["chars_to_synth"], len("Qui-Gon spoke."))
        self.assertGreater(im["est_seconds"], 0)
        # without a split function, the whole chapter is the safe upper bound
        im2 = pron.impact(self.conn, self.storage, "u1")
        self.assertGreater(im2["chars_to_synth"], im["chars_to_synth"])

    def test_impact_clears_after_the_reencode(self):
        jobs.render_book(self.conn, self.storage, self.speech, "u1", self.bid, voice="v1")
        pron.save(self.conn, "u1", [{"from": "Qui-Gon", "to": "Kwai-Gon"}])
        jobs.render_book(self.conn, self.storage, self.speech, "u1", self.bid,
                         voice="v1", only=[1])
        self.assertEqual(
            pron.impact(self.conn, self.storage, "u1",
                        split_for=lambda p: self.speech.split)["chapters_affected"], 0)


class TestPronRoutes(unittest.TestCase):
    def test_set_then_get_over_the_app(self):
        from host.app import App
        from tests.test_pluginhost import write_plugin
        tmp = tempfile.mkdtemp(prefix="fireside-pronapp-")
        root = os.path.join(tmp, "plugins")
        write_plugin(root, "storage", "mem", body=(
            "import hashlib\n"
            "class Plugin:\n"
            "    def __init__(self): self._d = {}\n"
            "    def put(self, data):\n"
            "        r = hashlib.sha256(data).hexdigest(); self._d[r] = data; return r\n"
            "    def has(self, ref): return ref in self._d\n"
            "    def get(self, ref): return self._d[ref]\n"
            "    def delete(self, ref): self._d.pop(ref, None)\n"
            "    def info(self): return {'kind': 'memory', 'durable': False}\n"))
        app = App(root, db_path=os.path.join(tmp, "a.db"),
                  storage_id="mem", require_auth=False)
        st, out = app.pron_set({"user": "u1"},
                               {"rules": [{"from": "Qui-Gon", "to": "Kwai-Gon"}]})
        self.assertEqual(st, 200)
        self.assertEqual(out["rules"], [{"from": "Qui-Gon", "to": "Kwai-Gon",
                                         "book": "", "author": "", "series": ""}])
        st, out = app.pron_get({"user": "u1"}, {})
        self.assertEqual(st, 200)
        self.assertEqual(out["rules"], [{"from": "Qui-Gon", "to": "Kwai-Gon",
                                         "book": "", "author": "", "series": ""}])
        st, out = app.pron_impact({"user": "u1"}, {})
        self.assertEqual(st, 200)
        self.assertEqual(out["chapters_affected"], 0)
        self.assertEqual(out["books"], [])


class TestGlyphsAndNumbers(unittest.TestCase):
    """A fic's own money symbols, and the numbers they sit next to."""

    RULES = [{"from": "\u029b", "to": "galleon", "book": "", "author": ""},
             {"from": "\u03b4", "to": "sickle", "book": "", "author": ""},
             {"from": "\u03ba", "to": "knut", "book": "", "author": ""}]

    def test_symbol_after_the_number_is_read(self):
        self.assertEqual(pron.spoken("50\u029b 30\u03b4 and 20\u03ba", self.RULES),
                         "50 galleon 30 sickle and 20 knut")

    def test_symbol_before_the_number_is_read(self):
        # a unit in front of its number reads number-first: fifty thousand galleon
        self.assertEqual(pron.spoken("\u029b50,000", self.RULES),
                         "fifty thousand galleon")

    def test_a_grouped_number_becomes_words(self):
        self.assertEqual(pron.expand_numbers("50,000"), "fifty thousand")
        self.assertEqual(pron.expand_numbers("1,250,000"),
                         "one million two hundred fifty thousand")

    def test_an_ungrouped_number_is_left_alone(self):
        self.assertEqual(pron.expand_numbers("chapter 12 in 1999"),
                         "chapter 12 in 1999")
        self.assertEqual(pron.expand_numbers("50000"), "50000")

    def test_a_unit_in_front_of_a_number_reads_number_first(self):
        self.assertEqual(pron.spoken("\u03ba50", self.RULES), "fifty knut")
        self.assertEqual(pron.spoken("\u029b50,000", self.RULES),
                         "fifty thousand galleon")

    def test_a_glyph_and_a_separate_number_are_not_one_amount(self):
        self.assertEqual(pron.spoken("50\u029b 30\u03b4", self.RULES),
                         "50 galleon 30 sickle")

    def test_a_multi_word_replacement_is_not_treated_as_a_unit(self):
        rules = [{"from": "\u03ba", "to": "knut piece", "book": "", "author": ""}]
        self.assertEqual(pron.spoken("\u03ba50", rules), "knut piece 50")

    def test_a_rule_still_keeps_out_of_longer_words(self):
        rules = [{"from": "cat", "to": "kat", "book": "", "author": ""}]
        self.assertEqual(pron.spoken("cats", rules), "cats")
        self.assertEqual(pron.spoken("cat", rules), "kat")


class TestScopes(unittest.TestCase):
    """Global, by author, by book: the narrowest wins."""

    def setUp(self):
        self.rules = [
            {"from": "\u029b", "to": "galleon", "book": "", "author": ""},
            {"from": "\u029b", "to": "coin", "book": "", "author": "mira"},
            {"from": "\u029b", "to": "crown", "book": "b1", "author": ""},
        ]

    def test_a_book_with_no_scope_gets_the_global_rule(self):
        got = pron.for_book(self.rules, "b9", "someone else")
        self.assertEqual([r["to"] for r in got], ["galleon"])

    def test_the_author_rule_wins_over_global(self):
        got = pron.for_book(self.rules, "b9", "Mira")
        self.assertEqual([r["to"] for r in got], ["coin"])

    def test_the_book_rule_wins_over_both(self):
        got = pron.for_book(self.rules, "b1", "mira")
        self.assertEqual([r["to"] for r in got], ["crown"])

    def test_a_rule_scoped_to_a_book_does_not_touch_another(self):
        rules = [{"from": "\u029b", "to": "crown", "book": "b1", "author": ""}]
        self.assertEqual(pron.for_book(rules, "b2", ""), [])
        self.assertEqual(len(pron.for_book(rules, "b1", "")), 1)

    def test_a_scoped_rule_only_tags_the_book_it_applies_to(self):
        rules = [{"from": "\u029b", "to": "crown", "book": "b1", "author": ""}]
        text = "\u029b50"
        self.assertTrue(pron.token_for(text, pron.for_book(rules, "b1", "")))
        self.assertEqual(pron.token_for(text, pron.for_book(rules, "b2", "")), "")


if __name__ == "__main__":
    unittest.main()
