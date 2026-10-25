"""Make bare `def test_x()` modules actually run in CI.

CI runs `python -m unittest discover -s tests -t .`, and unittest only collects
TestCase classes. Three test modules were written pytest-style (bare functions
with `assert`), so 26 tests - the whole pdf and docx reader suite included -
were counted as zero and never ran, while CI stayed green.

Rather than convert them one by one and leave the next author to make the same
mistake, this module collects every top-level `def test_...` in tests/ and runs
it as a real TestCase, named after the module and function. A bare function is
now collected automatically; nothing has to be remembered.
"""
import importlib.util
import pathlib
import re
import unittest

HERE = pathlib.Path(__file__).resolve().parent
SELF = pathlib.Path(__file__).name


def _bare_tests():
    """Yield (module_name, function_name, function) for pytest-style tests."""
    for path in sorted(HERE.glob("test_*.py")):
        if path.name == SELF:
            continue
        src = path.read_text(encoding="utf-8")
        if re.search(r"^def test_\w+", src, re.M) is None:
            continue
        spec = importlib.util.spec_from_file_location("bare_" + path.stem, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        for name, obj in sorted(vars(mod).items()):
            if name.startswith("test_") and callable(obj) and not isinstance(obj, type):
                yield path.stem, name, obj


def _runner(fn):
    def run(self):
        fn()
    return run


COLLECTED = list(_bare_tests())
Bare = type("BareFunctions", (unittest.TestCase,), {})
for _mod, _name, _fn in COLLECTED:
    setattr(Bare, "%s__%s" % (_mod, _name), _runner(_fn))


class Collection(unittest.TestCase):
    def test_bare_test_functions_are_found(self):
        """If this is zero, the collector broke and green means nothing again."""
        self.assertTrue(COLLECTED, "no bare test functions collected")

    def test_the_reader_suites_are_collected(self):
        mods = {m for m, _, _ in COLLECTED}
        for want in ("test_pdf_import", "test_docx_import"):
            self.assertIn(want, mods)
