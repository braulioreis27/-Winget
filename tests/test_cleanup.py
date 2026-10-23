"""Testes das regras conservadoras de limpeza de resíduos."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from wpm.cleanup import (
    _build_tokens,
    _match_name,
    delete_residual,
    directory_size,
    is_protected,
    normalize,
)
from wpm.errors import WpmError
from wpm.models import Residual


class NormalizationTests(unittest.TestCase):
    def test_normalize_removes_accents_and_punctuation(self) -> None:
        self.assertEqual(normalize("Configuração Rápida!"), "configuracao rapida")
        self.assertEqual(normalize("7-Zip"), "7 zip")

    def test_tokens_are_specific(self) -> None:
        tokens, publisher = _build_tokens("Mozilla Firefox", "Mozilla.Firefox")
        self.assertIn("firefox", tokens)
        self.assertIn("mozilla firefox", tokens)
        self.assertEqual(publisher, "mozilla")
        self.assertNotIn("mozilla", tokens)

    def test_generic_names_are_ignored(self) -> None:
        tokens, _ = _build_tokens("Temp", "Vendor.Temp")
        self.assertEqual(tokens, set())


class MatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tokens, _ = _build_tokens("Mozilla Firefox", "Mozilla.Firefox")

    def test_exact_match(self) -> None:
        matched, exact = _match_name("Firefox", self.tokens, exact_only=False)
        self.assertTrue(matched)
        self.assertTrue(exact)

    def test_partial_match(self) -> None:
        matched, exact = _match_name("Firefox Profile", self.tokens, exact_only=False)
        self.assertTrue(matched)
        self.assertFalse(exact)

    def test_vendor_folder_is_not_a_match(self) -> None:
        matched, _ = _match_name("Mozilla", self.tokens, exact_only=False)
        self.assertFalse(matched)
        matched, _ = _match_name("Mozilla Thunderbird", self.tokens, exact_only=False)
        self.assertFalse(matched)

    def test_exact_only_mode(self) -> None:
        matched, _ = _match_name("Firefox Profile", self.tokens, exact_only=True)
        self.assertFalse(matched)

    def test_7zip_variants(self) -> None:
        tokens, publisher = _build_tokens("7-Zip", "7zip.7zip")
        self.assertEqual(publisher, "7zip")
        for folder in ("7-Zip", "7zip", "7 Zip"):
            matched, exact = _match_name(folder, tokens, exact_only=True)
            self.assertTrue(matched, folder)
            self.assertTrue(exact, folder)


class ProtectionTests(unittest.TestCase):
    def test_home_and_cwd_are_protected(self) -> None:
        self.assertTrue(is_protected(Path.home()))
        self.assertTrue(is_protected(Path.cwd()))

    def test_windows_directory_is_protected(self) -> None:
        windows_dir = os.environ.get("WINDIR", "C:\\Windows")
        self.assertTrue(is_protected(Path(windows_dir)))

    def test_regular_temp_folder_is_not_protected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            candidate = Path(temporary) / "Firefox Profile"
            candidate.mkdir()
            self.assertFalse(is_protected(candidate))


class SizeAndDeleteTests(unittest.TestCase):
    def test_directory_size(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a.bin").write_bytes(b"abc")
            sub = root / "sub"
            sub.mkdir()
            (sub / "b.bin").write_bytes(b"12345")
            self.assertEqual(directory_size(root), 8)

    def test_permanent_delete_of_residual(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "AppData"
            target.mkdir()
            (target / "file.txt").write_text("x", encoding="utf-8")
            residual = Residual(path=target, exact=True)
            delete_residual(residual, permanent=True)
            self.assertFalse(target.exists())

    def test_delete_refuses_protected_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "Microsoft"
            target.mkdir()
            residual = Residual(path=target, exact=True)
            with self.assertRaises(WpmError):
                delete_residual(residual, permanent=True)
            self.assertTrue(target.exists())


if __name__ == "__main__":
    unittest.main()
