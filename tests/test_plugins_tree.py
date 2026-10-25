"""The real plugin tree, discovered the way the host discovers it.

No network: this only proves the shipped plugins under plugins/ load behind the
v2 contracts and keep reader order. The parsers and the providers themselves are
proven in test_sources / test_providers; step 2 adds no logic, it moves the
proven objects behind the plugin boundary.
"""

import os
import unittest

from host.pluginhost import discover

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGINS = os.path.join(ROOT, "plugins")


class TestReaderTree(unittest.TestCase):
    def test_specific_sources_first_generic_last(self):
        ids = [p.id for p in discover("reader", PLUGINS)]
        # pdf first: its magic bytes are unambiguous. Then docx before epub,
        # because both are zips and the docx reader asks a narrower question.
        self.assertEqual(ids, ["pdf", "docx", "epub", "ao3", "quantumbang", "web"])

    def test_reader_objects_expose_the_contract(self):
        for p in discover("reader", PLUGINS):
            o = p.obj
            for m in ("matches", "read", "settings", "credential_hosts"):
                self.assertTrue(callable(getattr(o, m, None)),
                                "%s missing %s" % (p.id, m))
            self.assertIsInstance(o.settings(), list)


class TestSpeechTree(unittest.TestCase):
    def test_every_speech_plugin_loads(self):
        self.assertEqual(set(discover("speech", PLUGINS)),
                         {"local", "openai", "azure", "minimax"})

    def test_speech_objects_expose_the_contract(self):
        for pid, p in discover("speech", PLUGINS).items():
            o = p.obj
            for m in ("synth", "list_voices", "cost_per_1m_chars", "chunks"):
                self.assertTrue(callable(getattr(o, m, None)),
                                "%s missing %s" % (pid, m))
            self.assertTrue(o.ext and o.mime, "%s has no file shape" % pid)
            self.assertIsInstance(o.needs_key, bool, "%s needs_key" % pid)


class TestStorageTree(unittest.TestCase):
    def test_local_backend_is_discovered(self):
        d = discover("storage", PLUGINS)
        self.assertIn("local", d)

    def test_storage_object_exposes_the_contract(self):
        o = discover("storage", PLUGINS)["local"].obj
        for m in ("put", "has", "get"):
            self.assertTrue(callable(getattr(o, m, None)), "storage missing %s" % m)


if __name__ == "__main__":

    unittest.main()
