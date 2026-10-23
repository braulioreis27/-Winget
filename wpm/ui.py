"""Interface de terminal: cores, tabelas, prompts e barra de progresso."""

from __future__ import annotations

import re
import sys
import threading
import time
import unicodedata
from collections.abc import Sequence
from typing import TextIO

from .errors import UserCancelled
from .models import Package
from .platform_win import terminal_width

RESET = "\x1b[0m"
STYLES = {
    "bold": "\x1b[1m",
    "dim": "\x1b[2m",
    "red": "\x1b[31m",
    "green": "\x1b[32m",
    "yellow": "\x1b[33m",
    "blue": "\x1b[34m",
    "magenta": "\x1b[35m",
    "cyan": "\x1b[36m",
    "grey": "\x1b[90m",
}

BANNER = r"""
 __        __  ____   __  __
 \ \      / / |  _ \ |  \/  |
  \ \ /\ / /  | |_) || |\/| |
   \ V  V /   |  __/ | |  | |
    \_/\_/    |_|    |_|  |_|
"""

_NUMBER = r"\d+(?:[.,]\d+)*"
_PERCENT_RE = re.compile(r"(\d{1,3})(?:[.,]\d+)?\s*%")
_SPEED_RE = re.compile(rf"({_NUMBER})\s*(B|KB|MB|GB|TB)\s*/\s*s", re.IGNORECASE)
_SIZE_RE = re.compile(
    rf"({_NUMBER})\s*(B|KB|MB|GB|TB)\s*/\s*({_NUMBER})\s*(B|KB|MB|GB|TB)",
    re.IGNORECASE,
)
_UNITS = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}


def display_width(text: str) -> int:
    """Largura visual aproximada, considerando caracteres largos e combinantes."""
    width = 0
    for char in text:
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return width


def truncate(text: str, width: int) -> str:
    """Corta o texto respeitando a largura visual e adiciona reticências."""
    if width <= 0:
        return ""
    if display_width(text) <= width:
        return text
    if width <= 3:
        return "." * width
    target = width - 3
    result: list[str] = []
    used = 0
    for char in text:
        char_width = 0 if unicodedata.combining(char) else (
            2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
        )
        if used + char_width > target:
            break
        result.append(char)
        used += char_width
    return "".join(result) + "..."


def pad(text: str, width: int) -> str:
    """Preenche à direita até a largura visual desejada."""
    return text + " " * max(0, width - display_width(text))


def format_size(size: int | None) -> str:
    """Formata bytes em uma unidade legível."""
    if size is None:
        return "?"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def _to_number(value: str) -> float:
    """Converte números no formato pt-BR (1.024,5) ou en-US (1,024.5)."""
    if "," in value and "." in value:
        if value.rfind(",") > value.rfind("."):
            value = value.replace(".", "").replace(",", ".")
        else:
            value = value.replace(",", "")
    elif "," in value:
        value = value.replace(",", ".")
    return float(value)


def parse_percent(text: str) -> int | None:
    """Extrai percentual de uma linha de progresso."""
    match = _PERCENT_RE.search(text)
    if match:
        return max(0, min(100, int(match.group(1))))
    size_match = _SIZE_RE.search(text)
    if size_match:
        done = _to_number(size_match.group(1)) * _UNITS[size_match.group(2).upper()]
        total = _to_number(size_match.group(3)) * _UNITS[size_match.group(4).upper()]
        if total > 0:
            return max(0, min(100, round(done * 100 / total)))
    return None


def parse_speed(text: str) -> str:
    """Extrai a velocidade de download, se presente."""
    match = _SPEED_RE.search(text)
    if not match:
        return ""
    return f"{_to_number(match.group(1)):g} {match.group(2).upper()}/s"


def parse_status(text: str) -> str:
    """Detecta a etapa atual da operação a partir do texto do winget."""
    lowered = text.casefold()
    if "hash" in lowered:
        return "Verificando hash"
    stages = (
        (("baixando", "downloading"), "Baixando"),
        (("instalando", "installing"), "Instalando"),
        (("desinstalando", "uninstalling"), "Desinstalando"),
        (("atualizando", "upgrading"), "Atualizando"),
        (("verificando", "verifying"), "Verificando"),
        (("extraindo", "extracting"), "Extraindo"),
    )
    for markers, label in stages:
        if any(marker in lowered for marker in markers):
            return label
    return ""


class Console:
    """Saída colorida e helpers interativos."""

    def __init__(self, color: bool = True, verbose: bool = False) -> None:
        self.color = color
        self.verbose = verbose
        self.out: TextIO = sys.stdout
        self.err: TextIO = sys.stderr
        self.is_tty = bool(getattr(self.out, "isatty", lambda: False)())

    def paint(self, text: str, *styles: str) -> str:
        if not self.color or not styles:
            return text
        prefix = "".join(STYLES.get(style, "") for style in styles)
        return f"{prefix}{text}{RESET}"

    def print(self, text: str = "", style: str | None = None, file: TextIO | None = None) -> None:
        target = file or self.out
        rendered = self.paint(text, style) if style else text
        print(rendered, file=target, flush=True)

    def info(self, message: str) -> None:
        self.print(f"[i] {message}", "cyan")

    def success(self, message: str) -> None:
        self.print(f"[OK] {message}", "green")

    def warn(self, message: str) -> None:
        self.print(f"[!] {message}", "yellow")

    def error(self, message: str) -> None:
        self.print(f"[X] {message}", "red", file=self.err)

    def hint(self, message: str, file: TextIO | None = None) -> None:
        self.print(f"    {message}", "grey", file=file)

    def title(self, text: str) -> None:
        self.print()
        self.print(f"== {text} ==", "bold")

    def rule(self, char: str = "-") -> None:
        self.print(char * min(terminal_width(), 78), "grey")

    def banner(self, version: str, winget_version: str = "") -> None:
        self.print(self.paint(BANNER.rstrip("\n"), "cyan"))
        self.print(f"WPM {version} - Gerenciador de pacotes para Windows", "bold")
        if winget_version:
            self.print(f"winget: {winget_version} | Python: {sys.version.split()[0]}", "grey")
        else:
            self.print(f"Python: {sys.version.split()[0]}", "grey")
        self.print()

    def table(
        self,
        headers: Sequence[str],
        rows: Sequence[Sequence[str]],
        *,
        index: bool = False,
        max_widths: Sequence[int] | None = None,
    ) -> None:
        """Renderiza uma tabela simples com alinhamento por largura visual."""
        all_rows = [[str(cell) for cell in row] for row in rows]
        headers = [str(header) for header in headers]
        if index:
            headers = ["#"] + headers
            all_rows = [[str(i + 1)] + row for i, row in enumerate(all_rows)]

        widths = [display_width(header) for header in headers]
        for row in all_rows:
            for i, cell in enumerate(row):
                widths[i] = max(widths[i], display_width(cell))

        if max_widths:
            limits = list(max_widths)
            if index:
                limits = [len(str(len(all_rows)))] + limits
            for i, limit in enumerate(limits):
                if i < len(widths):
                    widths[i] = min(widths[i], max(1, limit))

        available = terminal_width() - 2 * (len(widths) - 1)
        while sum(widths) > available:
            largest = max(range(len(widths)), key=lambda i: widths[i])
            if widths[largest] <= 4:
                break
            widths[largest] -= 1

        header_line = "  ".join(pad(truncate(h, widths[i]), widths[i]) for i, h in enumerate(headers))
        self.print(header_line.rstrip(), "bold")
        self.print("  ".join("-" * width for width in widths), "grey")
        for row in all_rows:
            cells = []
            for i, cell in enumerate(row):
                cells.append(pad(truncate(cell, widths[i]), widths[i]))
            self.print("  ".join(cells).rstrip())

    def packages_table(self, packages: Sequence[Package], show_available: bool = True) -> None:
        """Tabela padrão de pacotes."""
        headers = ["Nome", "ID", "Versão"]
        widths = [40, 48, 16]
        if show_available:
            headers.append("Disponível")
            widths.append(14)
        headers.append("Origem")
        widths.append(10)

        rows = []
        for package in packages:
            row = [package.display_name, package.id, package.version or "-"]
            if show_available:
                row.append(package.available or "-")
            row.append(package.source or "-")
            rows.append(row)
        self.table(headers, rows, index=True, max_widths=widths)

    def ask(self, prompt: str, default: str = "", required: bool = False) -> str:
        """Solicita texto ao usuário."""
        if not self.is_tty:
            if default:
                return default
            raise UserCancelled("Entrada interativa indisponível. Informe os parâmetros na linha de comando.")
        suffix = f" [{default}]" if default else ""
        while True:
            try:
                answer = input(self.paint(f"{prompt}{suffix}: ", "cyan")).strip()
            except (EOFError, KeyboardInterrupt) as exc:
                raise UserCancelled("Operação cancelada.") from exc
            if answer:
                return answer
            if default:
                return default
            if not required:
                return ""
            self.warn("Este campo é obrigatório.")

    def confirm(self, prompt: str, default: bool = False) -> bool:
        """Pergunta sim/não, com padrão seguro."""
        if not self.is_tty:
            return default
        options = "[S/n]" if default else "[s/N]"
        while True:
            try:
                answer = input(self.paint(f"{prompt} {options}: ", "yellow")).strip().lower()
            except (EOFError, KeyboardInterrupt) as exc:
                raise UserCancelled("Operação cancelada.") from exc
            if not answer:
                return default
            if answer in ("s", "sim", "y", "yes"):
                return True
            if answer in ("n", "nao", "não", "no"):
                return False
            self.warn("Responda com 's' ou 'n'.")

    def choose(self, prompt: str, count: int, allow_cancel: bool = True) -> int | None:
        """Seleciona um item pelo número (retorna índice base zero)."""
        if count <= 0:
            return None
        if not self.is_tty:
            if count == 1:
                return 0
            raise UserCancelled("Seleção interativa necessária. Use --id para ser específico.")
        for _ in range(5):
            try:
                answer = input(self.paint(f"{prompt} (1-{count}{', Enter cancela' if allow_cancel else ''}): ", "cyan")).strip()
            except (EOFError, KeyboardInterrupt) as exc:
                raise UserCancelled("Operação cancelada.") from exc
            if not answer and allow_cancel:
                return None
            if answer.isdigit() and 1 <= int(answer) <= count:
                return int(answer) - 1
            self.warn(f"Digite um número entre 1 e {count}.")
        raise UserCancelled("Número de tentativas excedido.")

    def select_many(self, prompt: str, count: int) -> list[int]:
        """Seleciona vários itens por número; aceita 'todos'."""
        if count <= 0:
            return []
        if not self.is_tty:
            return []
        for _ in range(5):
            try:
                answer = input(
                    self.paint(
                        f"{prompt} (ex.: 1,3 / 'todos' / Enter cancela): ",
                        "cyan",
                    )
                ).strip().lower()
            except (EOFError, KeyboardInterrupt) as exc:
                raise UserCancelled("Operação cancelada.") from exc
            if not answer:
                return []
            if answer in ("todos", "all", "*"):
                return list(range(count))
            selected: set[int] = set()
            invalid = False
            for part in re.split(r"[,\s;]+", answer):
                if not part:
                    continue
                if part.isdigit() and 1 <= int(part) <= count:
                    selected.add(int(part) - 1)
                else:
                    invalid = True
                    break
            if not invalid and selected:
                return sorted(selected)
            self.warn(f"Seleção inválida. Use números entre 1 e {count}.")
        raise UserCancelled("Número de tentativas excedido.")


class Progress:
    """Barra de progresso para downloads/instalações do winget."""

    def __init__(self, console: Console, label: str = "winget") -> None:
        self.console = console
        self.label = label
        self.enabled = console.is_tty
        self.percent: int | None = None
        self.speed = ""
        self.status = ""
        self.bytes = 0
        self.rate = 0.0
        self.started = time.monotonic()
        self._last_render = 0.0
        self._last_length = 0
        self._rendered = False
        self._last_bytes = 0
        self._last_bytes_at = self.started
        self._lock = threading.Lock()

    def feed(self, text: str) -> None:
        if not self.enabled:
            return
        percent = parse_percent(text)
        speed = parse_speed(text)
        status = parse_status(text)
        changed = False
        if percent is not None and percent != self.percent:
            self.percent = percent
            changed = True
        if speed and speed != self.speed:
            self.speed = speed
            changed = True
        if status and status != self.status:
            self.status = status
            changed = True
        if not changed and self.percent is None:
            return
        if changed:
            self._render()
        elif time.monotonic() - self._last_render > 0.1:
            self._render()

    def set_bytes(self, downloaded: int) -> None:
        """Atualiza a barra com bytes baixados, mantendo a velocidade média."""
        if not self.enabled:
            return
        now = time.monotonic()
        downloaded = max(0, downloaded)
        if downloaded > self.bytes:
            self.bytes = downloaded
        elapsed = now - self._last_bytes_at
        if elapsed >= 0.5:
            delta = max(0, downloaded - self._last_bytes)
            self.rate = delta / elapsed
            self._last_bytes = downloaded
            self._last_bytes_at = now
            self._render()

    def _line(self) -> str:
        width = max(10, min(30, terminal_width() - 55))
        if self.percent is None:
            spin = "|/-\\"[int((time.monotonic() - self.started) * 8) % 4]
            bar = spin + " " * (width - 1)
            percent_text = "   "
        else:
            filled = int(width * self.percent / 100)
            bar = "#" * filled + "-" * (width - filled)
            percent_text = f"{self.percent:3d}%"
        parts = [self.label[:18], f"[{bar}]", percent_text]
        if self.bytes:
            parts.append(format_size(self.bytes))
        if self.speed:
            parts.append(self.speed)
        elif self.rate:
            parts.append(f"{format_size(int(self.rate))}/s")
        if self.status and self.status.lower() not in self.label.lower():
            parts.append(self.status)
        line = " ".join(parts)
        plain = truncate(line, terminal_width() - 1)
        return plain + " " * max(0, width + 20 - display_width(plain))

    def _render(self) -> None:
        if not self.enabled:
            return
        now = time.monotonic()
        with self._lock:
            if now - self._last_render < 0.05:
                return
            line = self._line()
            self._last_render = now
            self._last_length = max(self._last_length, display_width(line))
            self.console.out.write(
                "\r" + line + (" " * max(0, self._last_length - display_width(line)))
            )
            self.console.out.flush()
            self._rendered = True

    def finish(self) -> None:
        """Limpa a linha da barra para que a mensagem final fique limpa."""
        if not self.enabled or not self._rendered:
            return
        with self._lock:
            self.console.out.write("\r" + " " * self._last_length + "\r")
            self.console.out.flush()
            self._rendered = False
