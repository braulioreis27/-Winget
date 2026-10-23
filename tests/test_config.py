"""Testes de persistência e validação da configuração."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from wpm.config import Config
from wpm.errors import ConfigurationError


class ConfigTests(unittest.TestCase):
    def test_defaults(self) -> None:
        config = Config()
        self.assertTrue(config.silent_install)
        self.assertEqual(config.source, "auto")
        self.assertEqual(config.timeout, 1800)
        self.assertTrue(str(config.download_path).endswith("wpm"))

    def test_save_and_load_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            original = Config(silent_install=False, timeout=600, search_limit=10, source="msstore")
            original.save(path)
            loaded = Config.load(path)
            self.assertFalse(loaded.silent_install)
            self.assertEqual(loaded.timeout, 600)
            self.assertEqual(loaded.search_limit, 10)
            self.assertEqual(loaded.source, "msstore")
            self.assertEqual(loaded.load_warning, "")

    def test_invalid_json_is_backed_up(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text("{invalido", encoding="utf-8")
            config = Config.load(path)
            self.assertTrue(config.load_warning)
            self.assertTrue(list(Path(temporary).glob("config.invalid-*.json")))

    def test_invalid_values_fall_back_to_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text(json.dumps({"color": "rainbow"}), encoding="utf-8")
            config = Config.load(path)
            self.assertEqual(config.color, "auto")
            self.assertTrue(config.load_warning)

    def test_unknown_keys_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text(
                json.dumps({"desconhecida": 1, "silent_install": False}),
                encoding="utf-8",
            )
            config = Config.load(path)
            self.assertFalse(config.silent_install)
            self.assertFalse(hasattr(config, "desconhecida"))

    def test_validation(self) -> None:
        config = Config()
        with self.assertRaises(ConfigurationError):
            config.set("color", "rainbow")
        with self.assertRaises(ConfigurationError):
            config.set("timeout", 5)
        with self.assertRaises(ConfigurationError):
            config.set("search_limit", 0)
        with self.assertRaises(ConfigurationError):
            config.set("source", " ")
        with self.assertRaises(ConfigurationError):
            config.set("silent_install", "sim")

    def test_set_normalizes_values(self) -> None:
        config = Config()
        config.set("color", "ALWAYS")
        config.set("log_level", "debug")
        self.assertEqual(config.color, "always")
        self.assertEqual(config.log_level, "DEBUG")


if __name__ == "__main__":
    unittest.main()
