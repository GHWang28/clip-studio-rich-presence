"""CLI helpers that have to work on a Windows console."""

import io
import sys
import unittest
from unittest import mock

from csprpc import cli


class StatusMarkTest(unittest.TestCase):
    def test_utf8_keeps_the_ticks(self):
        with mock.patch.object(sys, "stdout", mock.Mock(encoding="utf-8")):
            self.assertEqual(cli._status_mark(True), "\u2713")
            self.assertEqual(cli._status_mark(False), "\u2717")
            self.assertEqual(cli._status_mark(None), "!")

    def test_cp1252_falls_back_to_ascii(self):
        # This is the encoding of the Windows runner's console, and of
        # cmd.exe on most Western installs. U+2713 is not in that map.
        with mock.patch.object(sys, "stdout", mock.Mock(encoding="cp1252")):
            self.assertEqual(cli._status_mark(True), "OK")
            self.assertEqual(cli._status_mark(False), "X")
            self.assertEqual(cli._status_mark(None), "!")

    def test_redirected_stdout_keeps_the_ticks(self):
        # StringIO (the GUI Diagnostics panel) has no encoding.
        with mock.patch.object(sys, "stdout", io.StringIO()):
            self.assertEqual(cli._status_mark(True), "\u2713")

    def test_printing_on_cp1252_does_not_raise(self):
        class Cp1252Stdout(io.TextIOBase):
            encoding = "cp1252"

            def __init__(self):
                self.buf = io.BytesIO()

            def write(self, text):
                self.buf.write(text.encode(self.encoding))
                return len(text)

            def flush(self):
                pass

        sink = Cp1252Stdout()
        with mock.patch.object(sys, "stdout", sink):
            for ok in (True, False, None):
                print("{} {}".format(cli._status_mark(ok), "Windows"))


if __name__ == "__main__":
    unittest.main()
