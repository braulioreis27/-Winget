"""Testes do parser das tabelas do winget (pt-BR e en-US)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from wpm.models import CommandResult
from wpm.winget import Winget, _directory_total, parse_table

PT_SEARCH = """Nome                               ID                        Versão          Correspondência Origem\r
---------------------------------------------------------------------------------------------------\r
7-Zip                              7zip.7zip                 26.05           Moniker: 7zip   winget\r
Advanced Archive Password Recovery Elcomsoft.ArchivePassword 4.66.266.6965   Tag: 7zip       winget\r
NanaZip                            M2Team.NanaZip            7.0.1873.0      Tag: 7zip       winget\r
7 atualizações disponíveis.\r
"""

EN_SEARCH = """Name                          Id                        Version      Source
--------------------------------------------------------------------------------
Mozilla Firefox               Mozilla.Firefox           134.0.2      winget
Google Chrome                 Google.Chrome             131.0.6778.86 winget
"""

def _render_table(headers: list[str], rows: list[list[str]], widths: list[int]) -> str:
    def render(cells: list[str]) -> str:
        return "".join(cell.ljust(width) for cell, width in zip(cells, widths)).rstrip()

    lines = [render(headers), "-" * sum(widths)]
    lines.extend(render(row) for row in rows)
    return "\n".join(lines) + "\n"


PT_LIST = _render_table(
    ["Nome", "ID", "Versão", "Disponível", "Origem"],
    [
        ["GitHub Desktop", "GitHub.GitHubDesktop", "3.6.7", "3.6.8", "winget"],
        ["Microsoft Edge", "Microsoft.Edge", "155.0.4283.62", "156.0.4314.42", "winget"],
        [
            "MSIX Thing",
            "MSIX\\Microsoft.Thing_1.0.0.0_x64__8wekyb3d8bbwe",
            "1.0.0.0",
            "",
            "",
        ],
    ],
    [40, 62, 20, 16, 10],
)

PT_UPGRADE = """Nome                          ID                              Versão        Disponível    Origem
------------------------------------------------------------------------------------------------
GitHub Desktop                GitHub.GitHubDesktop            3.6.7         3.6.8         winget
Python Launcher               Python.Launcher                 3.12.10       3.14.7        winget
7 atualizações disponíveis.
"""


class ParseTableTests(unittest.TestCase):
    def test_parse_portuguese_search(self) -> None:
        packages = parse_table(PT_SEARCH)
        self.assertEqual(len(packages), 3)
        self.assertEqual(packages[0].name, "7-Zip")
        self.assertEqual(packages[0].id, "7zip.7zip")
        self.assertEqual(packages[0].version, "26.05")
        self.assertEqual(packages[0].source, "winget")
        self.assertEqual(packages[1].id, "Elcomsoft.ArchivePassword")

    def test_parse_english_search(self) -> None:
        packages = parse_table(EN_SEARCH)
        self.assertEqual([package.id for package in packages], ["Mozilla.Firefox", "Google.Chrome"])
        self.assertEqual(packages[1].version, "131.0.6778.86")

    def test_parse_upgrade_with_summary_line(self) -> None:
        packages = parse_table(PT_UPGRADE)
        self.assertEqual(len(packages), 2)
        self.assertEqual(packages[0].available, "3.6.8")
        self.assertTrue(packages[0].is_upgradable)

    def test_parse_list_keeps_ids_with_backslashes(self) -> None:
        packages = parse_table(PT_LIST)
        self.assertEqual(len(packages), 3)
        self.assertEqual(packages[2].name, "MSIX Thing")
        self.assertIn("MSIX\\", packages[2].id)
        self.assertFalse(packages[2].is_winget_managed)

    def test_limit_is_respected(self) -> None:
        packages = parse_table(PT_SEARCH, limit=1)
        self.assertEqual(len(packages), 1)

    def test_empty_and_malformed_outputs(self) -> None:
        self.assertEqual(parse_table(""), [])
        self.assertEqual(parse_table("Nenhum pacote encontrou os critérios."), [])
        self.assertEqual(parse_table("sem cabeçalho\n1 2 3"), [])

    def test_header_is_case_insensitive(self) -> None:
        header = f"{'name':<12}{'id':<13}{'version':<13}source"
        row = f"{'App':<12}{'Pub.App':<13}{'1.0':<13}winget"
        output = "\n".join([header, "-" * 50, row])
        packages = parse_table(output)
        self.assertEqual(len(packages), 1)
        self.assertEqual(packages[0].name, "App")
        self.assertEqual(packages[0].id, "Pub.App")
        self.assertEqual(packages[0].version, "1.0")


class ResultClassificationTests(unittest.TestCase):
    def test_benign_already_installed(self) -> None:
        result = CommandResult(
            args=(),
            returncode=-1978335189,
            output=(
                "Nenhuma atualização aplicável.\n"
                "Foi encontrado um pacote existente já instalado.\n"
                "Nenhuma versão de pacote mais recente está disponível nas origens configuradas."
            ),
        )
        self.assertTrue(Winget.is_benign(result))
        self.assertFalse(Winget.is_network_failure(result))

    def test_network_failure(self) -> None:
        result = CommandResult(
            args=(),
            returncode=-1978335224,
            output="Falha na conexão: 0x80072EE2 (tempo limite).",
        )
        self.assertTrue(Winget.is_network_failure(result))
        self.assertFalse(Winget.is_benign(result))

    def test_success_is_neither(self) -> None:
        result = CommandResult(args=(), returncode=0)
        self.assertFalse(Winget.is_benign(result))
        self.assertFalse(Winget.is_network_failure(result))


class DirectoryTotalTests(unittest.TestCase):
    def test_counts_nested_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sub").mkdir()
            (root / "sub" / "nested.bin").write_bytes(b"x" * 100)
            (root / "root.bin").write_bytes(b"y" * 50)
            self.assertEqual(_directory_total(root), 150)

    def test_missing_directory_is_zero(self) -> None:
        self.assertEqual(_directory_total(Path("Z:/caminho/inexistente/wpm")), 0)


if __name__ == "__main__":
    unittest.main()
