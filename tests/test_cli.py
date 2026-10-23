"""Testes de parsing de argumentos e helpers do CLI."""

from __future__ import annotations

import hashlib
import tempfile
import time
import unittest
from pathlib import Path

from wpm.cli import (
    _absolute,
    _coerce_config_value,
    _detect_downloaded_files,
    _filter_packages,
    _preview,
    _write_checksums,
    build_parser,
)
from wpm.errors import ConfigurationError
from wpm.models import Package


class ParserTests(unittest.TestCase):
    def test_aliases_and_global_flags(self) -> None:
        cases = [
            (["find", "firefox"], "find"),
            (["add", "7zip.7zip"], "add"),
            (["fetch", "7zip.7zip"], "fetch"),
            (["ls"], "ls"),
            (["upgrade"], "upgrade"),
            (["rm"], "rm"),
            (["purge"], "purge"),
            (["check"], "check"),
        ]
        for argv, expected in cases:
            with self.subTest(argv=argv):
                args = build_parser().parse_args(argv)
                self.assertEqual(args.command, expected)

    def test_global_flag_before_and_after_subcommand(self) -> None:
        before = build_parser().parse_args(
            ["--no-color", "-v", "-y", "--timeout", "90", "search", "firefox"]
        )
        after = build_parser().parse_args(["search", "firefox", "--no-color", "-v", "-y"])
        self.assertTrue(before.no_color)
        self.assertTrue(before.verbose)
        self.assertTrue(before.assume_yes)
        self.assertEqual(before.timeout, 90)
        self.assertTrue(after.no_color)
        self.assertTrue(after.verbose)
        self.assertTrue(after.assume_yes)
        self.assertIsNone(after.timeout)

    def test_timeout_and_source(self) -> None:
        args = build_parser().parse_args(
            ["search", "firefox", "--timeout", "120", "--source", "msstore"]
        )
        self.assertEqual(args.timeout, 120)
        self.assertEqual(args.source, "msstore")


class HelperTests(unittest.TestCase):
    def test_filter_packages(self) -> None:
        packages = [
            Package(name="Mozilla Firefox", id="Mozilla.Firefox"),
            Package(name="Google Chrome", id="Google.Chrome"),
        ]
        self.assertEqual(len(_filter_packages(packages, "firefox")), 1)
        self.assertEqual(len(_filter_packages(packages, "mozilla firefox")), 1)
        self.assertEqual(len(_filter_packages(packages, "mozilla chrome")), 0)
        self.assertEqual(len(_filter_packages(packages, "")), 2)

    def test_coerce_config_value(self) -> None:
        for raw in ("1", "true", "sim", "s", "yes", "on", "TRUE"):
            self.assertIs(_coerce_config_value("silent_install", raw), True)
        for raw in ("0", "false", "nao", "não", "n", "no", "off"):
            self.assertIs(_coerce_config_value("update_sources", raw), False)
        self.assertEqual(_coerce_config_value("timeout", "600"), 600)
        self.assertEqual(_coerce_config_value("source", "winget"), "winget")
        with self.assertRaises(ConfigurationError):
            _coerce_config_value("silent_install", "talvez")
        with self.assertRaises(ConfigurationError):
            _coerce_config_value("timeout", "abc")

    def test_absolute_and_preview(self) -> None:
        self.assertEqual(_absolute("~"), Path.home().resolve())
        self.assertIn("a b", _preview(["a b", "c"]))


class DownloadHelperTests(unittest.TestCase):
    def test_detect_new_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            before = {item.resolve() for item in directory.iterdir() if item.is_file()}
            new_file = directory / "novo.msi"
            new_file.write_bytes(b"x")
            detected = _detect_downloaded_files(directory, before, time.time())
            self.assertIn(new_file.resolve(), detected)

    def test_detect_recent_file_without_snapshot_match(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            existing = directory / "baixado.msi"
            existing.write_bytes(b"x")
            before = {item.resolve() for item in directory.iterdir() if item.is_file()}
            detected = _detect_downloaded_files(directory, before, time.time())
            self.assertIn(existing.resolve(), detected)

    def test_write_checksums(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            first = directory / "a.msi"
            first.write_bytes(b"abc")
            package = Package(name="7-Zip", id="7zip.7zip")
            checksum_file = _write_checksums(package, [first], directory)
            self.assertTrue(checksum_file.exists())
            self.assertEqual(checksum_file.suffix, ".sha256")
            content = checksum_file.read_text(encoding="utf-8")
            expected = hashlib.sha256(b"abc").hexdigest()
            self.assertIn(expected, content)
            self.assertIn("a.msi", content)


if __name__ == "__main__":
    unittest.main()
