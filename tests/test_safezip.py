"""An uploaded zip is read with a ceiling on what any one member may inflate to."""

import io
import unittest
import zipfile

from host import safezip


def make_zip(members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return zipfile.ZipFile(io.BytesIO(buf.getvalue()))


class TestSafeZip(unittest.TestCase):
    def test_a_normal_member_reads_back(self):
        z = make_zip({"a.xml": b"<a/>"})
        self.assertEqual(safezip.read(z, "a.xml"), b"<a/>")

    def test_a_member_that_inflates_past_the_limit_is_refused(self):
        z = make_zip({"bomb.xml": b"0" * 50000})        # compresses to ~100 bytes
        with self.assertRaises(zipfile.BadZipFile):
            safezip.read(z, "bomb.xml", limit=1000)

    def test_a_member_exactly_at_the_limit_is_allowed(self):
        z = make_zip({"edge": b"x" * 1000})
        self.assertEqual(len(safezip.read(z, "edge", limit=1000)), 1000)

    def test_the_default_limit_is_read_when_called_not_when_defined(self):
        from unittest import mock
        z = make_zip({"m": b"y" * 5000})
        with mock.patch.object(safezip, "MEMBER_LIMIT", 100):
            with self.assertRaises(zipfile.BadZipFile):
                safezip.read(z, "m")

    def test_a_docx_whose_body_is_a_bomb_is_refused_not_parsed(self):
        from unittest import mock
        from host import docx_import
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as w:
            w.writestr("word/document.xml", b"<a>" + b"x" * 5000 + b"</a>")
        with mock.patch.object(safezip, "MEMBER_LIMIT", 100):
            with self.assertRaises(zipfile.BadZipFile):
                docx_import.extract(buf.getvalue())

    def test_a_missing_member_is_still_a_key_error(self):
        with self.assertRaises(KeyError):
            safezip.read(make_zip({"a": b"1"}), "nope")


if __name__ == "__main__":
    unittest.main()
