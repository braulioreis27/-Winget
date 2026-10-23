"""Testes de formatação, parsing de progresso e tabelas da interface."""

from __future__ import annotations

import io
import time
import unittest

from wpm.models import Package
from wpm.ui import (
    Console,
    Progress,
    display_width,
    format_size,
    pad,
    parse_percent,
    parse_speed,
    parse_status,
    truncate,
)


class FormatTests(unittest.TestCase):
    def test_format_size(self) -> None:
        self.assertEqual(format_size(None), "?")
        self.assertEqual(format_size(0), "0 B")
        self.assertEqual(format_size(1536), "1.5 KB")
        self.assertEqual(format_size(5 * 1024**3), "5.0 GB")

    def test_truncate_and_pad_respect_visual_width(self) -> None:
        self.assertEqual(truncate("abcdef", 10), "abcdef")
        self.assertEqual(truncate("abcdef", 5), "ab...")
        self.assertEqual(truncate("abc", 3), "abc")
        self.assertEqual(truncate("abcdef", 2), "..")
        self.assertEqual(pad("ab", 5), "ab   ")
        self.assertEqual(display_width("abc"), 3)


class ProgressParsingTests(unittest.TestCase):
    def test_percent(self) -> None:
        self.assertEqual(parse_percent("  45%"), 45)
        self.assertEqual(parse_percent("100%"), 100)
        self.assertEqual(parse_percent("150%"), 100)
        self.assertIsNone(parse_percent("sem progresso"))

    def test_percent_from_sizes(self) -> None:
        self.assertEqual(parse_percent("12.3 MB / 45.6 MB"), 27)
        self.assertEqual(parse_percent("1.024,0 KB / 2.048,0 KB"), 50)

    def test_speed(self) -> None:
        self.assertEqual(parse_speed("1.2 MB/s"), "1.2 MB/s")
        self.assertEqual(parse_speed("2,5 MB / s"), "2.5 MB/s")
        self.assertEqual(parse_speed("nada"), "")

    def test_status(self) -> None:
        self.assertEqual(parse_status("Baixando 7-Zip"), "Baixando")
        self.assertEqual(parse_status("Installing package"), "Instalando")
        self.assertEqual(parse_status("Verifying hash"), "Verificando hash")
        self.assertEqual(parse_status("outra coisa"), "")

    def test_progress_renders_on_tty(self) -> None:
        console = Console(color=False)
        console.is_tty = True
        stream = io.StringIO()
        console.out = stream
        progress = Progress(console)
        progress.feed("Baixando 42% 1.5 MB/s")
        progress._last_render = 0.0
        progress.feed("Baixando 80% 2.0 MB/s")
        progress.finish()
        rendered = stream.getvalue()
        self.assertIn("42%", rendered)
        self.assertIn("80%", rendered)
        self.assertIn("1.5 MB/s", rendered)
        self.assertIn("\r", rendered)

    def test_progress_shows_downloaded_bytes(self) -> None:
        console = Console(color=False)
        console.is_tty = True
        stream = io.StringIO()
        console.out = stream
        progress = Progress(console)
        progress._last_render = 0.0
        progress._last_bytes_at = time.monotonic() - 1.0
        progress.set_bytes(1024 * 1024)
        progress.finish()
        self.assertIn("1.0 MB", stream.getvalue())


class TableTests(unittest.TestCase):
    def test_table_renders_headers_and_rows(self) -> None:
        console = Console(color=False)
        stream = io.StringIO()
        console.out = stream
        console.table(["Nome", "ID"], [["Firefox", "Mozilla.Firefox"]], index=True)
        rendered = stream.getvalue()
        self.assertIn("#", rendered)
        self.assertIn("Nome", rendered)
        self.assertIn("Mozilla.Firefox", rendered)

    def test_packages_table(self) -> None:
        console = Console(color=False)
        stream = io.StringIO()
        console.out = stream
        package = Package(name="7-Zip", id="7zip.7zip", version="26.05", source="winget")
        console.packages_table([package], show_available=False)
        rendered = stream.getvalue()
        self.assertIn("7zip.7zip", rendered)
        self.assertIn("26.05", rendered)


if __name__ == "__main__":
    unittest.main()
