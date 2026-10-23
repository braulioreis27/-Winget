"""Testes de execução de subprocessos, streaming e timeout."""

from __future__ import annotations

import sys
import time
import unittest

from wpm.errors import WingetNotFoundError
from wpm.executor import decode_output, run_capture, sanitize, stream_command


class ExecutorTests(unittest.TestCase):
    def test_run_capture(self) -> None:
        result = run_capture(
            [sys.executable, "-X", "utf8", "-c", "print('versão ok')"],
            timeout=30,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("versão ok", result.output)
        self.assertFalse(result.timed_out)
        self.assertTrue(result.succeeded)

    def test_run_capture_missing_executable(self) -> None:
        with self.assertRaises(WingetNotFoundError):
            run_capture(["wpm-executavel-inexistente-xyz"], timeout=10)

    def test_run_capture_timeout(self) -> None:
        started = time.monotonic()
        result = run_capture(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            timeout=0.5,
        )
        elapsed = time.monotonic() - started
        self.assertTrue(result.timed_out)
        self.assertLess(elapsed, 15)

    def test_stream_command_splits_cr_and_lf(self) -> None:
        script = (
            "import sys\n"
            "sys.stdout.write('inicio\\n')\n"
            "sys.stdout.write(' 10%\\r')\n"
            "sys.stdout.write(' 50%\\r')\n"
            "sys.stdout.write('fim\\n')\n"
            "sys.stdout.flush()\n"
        )
        received: list[str] = []
        result = stream_command(
            [sys.executable, "-X", "utf8", "-c", script],
            received.append,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("inicio", received)
        self.assertTrue(any("10%" in line for line in received))
        self.assertTrue(any("50%" in line for line in received))
        self.assertIn("fim", received)

    def test_sanitize_removes_ansi(self) -> None:
        self.assertEqual(sanitize("\x1b[32mok\x1b[0m"), "ok")
        self.assertEqual(sanitize("linha\x00com controle"), "linhacom controle")

    def test_decode_output_fallbacks(self) -> None:
        self.assertEqual(decode_output("ação".encode("utf-8")), "ação")
        self.assertEqual(decode_output("ação".encode("cp1252")), "ação")


if __name__ == "__main__":
    unittest.main()
