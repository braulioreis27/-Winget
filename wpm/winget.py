"""Backend de integração com o Windows Package Manager (winget)."""

from __future__ import annotations

import logging
import os
import re
import threading
from pathlib import Path
from typing import Sequence

from .config import Config
from .errors import NetworkError, OperationFailedError, WingetNotFoundError
from .executor import run_capture, stream_command
from .models import CommandResult, Package
from .platform_win import find_winget
from .ui import Console, Progress, truncate

LOGGER = logging.getLogger(f"wpm.{__name__.rsplit('.', 1)[-1]}")

_HEADER_ALIASES = {
    "name": ("name", "nome"),
    "id": ("id",),
    "version": ("version", "versão", "versao"),
    "available": ("available", "disponível", "disponivel"),
    "match": ("match", "correspondência", "correspondencia"),
    "source": ("source", "origem"),
}
_ALIAS_TO_KEY = {alias: key for key, aliases in _HEADER_ALIASES.items() for alias in aliases}
_HEADER_RE = re.compile(
    r"(?<!\w)(" + "|".join(sorted(map(re.escape, _ALIAS_TO_KEY), key=len, reverse=True)) + r")(?!\w)",
    re.IGNORECASE,
)

_ERROR_CODES = {
    0x8A150001: "Erro interno do winget.",
    0x8A150002: "Argumentos inválidos para o winget.",
    0x8A150003: "O comando do winget falhou.",
    0x8A150006: "Falha ao executar o instalador (permissões/UAC?).",
    0x8A150007: "Versão do Windows não suportada pelo pacote.",
    0x8A150008: "Falha no download do instalador.",
    0x8A150009: "Nenhum instalador aplicável foi encontrado.",
    0x8A150014: "Nenhum pacote encontrado.",
    0x8A15002B: "Nenhuma atualização aplicável.",
}

_BENIGN_MARKERS = (
    "já está instalado",
    "ja esta instalado",
    "já instalado",
    "ja instalado",
    "pacote existente já instalado",
    "pacote existente ja instalado",
    "already installed",
    "nenhuma atualização aplicável",
    "nenhuma atualizacao aplicavel",
    "no applicable upgrade",
    "não há atualizações disponíveis",
    "nao ha atualizacoes disponiveis",
    "no newer package versions",
    "nenhuma versão de pacote mais recente",
    "nenhuma versao de pacote mais recente",
    "nenhuma versão mais recente",
    "nenhuma versao mais recente",
    "no available upgrade",
    "already up to date",
)

_NETWORK_MARKERS = (
    "0x80072",
    "falha na conexão",
    "falha na conexao",
    "não foi possível conectar",
    "nao foi possivel conectar",
    "network",
    "internet",
    "connection",
    "tempo limite",
    "timed out",
)

_NO_RESULTS_MARKERS = (
    "nenhum pacote encontrou",
    "nenhum pacote encontrado",
    "no package found",
    "no packages found",
    "no installed package",
)


def _is_separator(line: str) -> bool:
    stripped = line.strip()
    return len(stripped) >= 3 and all(char in "-=─━ " for char in stripped)


def _directory_total(path: Path, depth: int = 0, max_depth: int = 3) -> int:
    """Soma arquivos de uma pasta, com profundidade limitada e sem seguir links."""
    total = 0
    try:
        with os.scandir(path) as iterator:
            for entry in iterator:
                try:
                    if entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
                    elif entry.is_dir(follow_symlinks=False) and depth < max_depth:
                        total += _directory_total(Path(entry.path), depth + 1, max_depth)
                except OSError:
                    continue
    except OSError:
        return 0
    return total


def _find_header(lines: Sequence[str]) -> tuple[int, list[tuple[str, int]]] | None:
    """Localiza o cabeçalho e as posições das colunas (pt-BR e en-US)."""
    for index, line in enumerate(lines[:25]):
        tokens: list[tuple[int, str]] = []
        for match in _HEADER_RE.finditer(line):
            key = _ALIAS_TO_KEY.get(match.group(0).casefold())
            if key:
                tokens.append((match.start(), key))
        keys = {key for _, key in tokens}
        if "name" not in keys or "id" not in keys:
            continue
        seen: set[str] = set()
        columns: list[tuple[str, int]] = []
        for position, key in tokens:
            if key in seen:
                continue
            seen.add(key)
            columns.append((key, position))
        return index, columns
    return None


def parse_table(output: str, limit: int = 500) -> list[Package]:
    """Converte a tabela textual do winget em objetos Package."""
    lines = output.splitlines()
    header = _find_header(lines)
    if header is None:
        return []
    header_index, columns = header
    packages: list[Package] = []

    for raw_line in lines[header_index + 1 :]:
        line = raw_line.rstrip()
        if not line.strip():
            if packages:
                break
            continue
        if _is_separator(line):
            continue

        cells: dict[str, str] = {}
        for position, (key, start) in enumerate(columns):
            end = columns[position + 1][1] if position + 1 < len(columns) else len(line)
            cells[key] = line[start:end].strip()

        package_id = cells.get("id", "")
        if not cells.get("name") or not package_id:
            if packages:
                break
            continue
        # Linhas de resumo ("7 atualizações disponíveis.") não têm um ID válido.
        if any(char.isspace() for char in package_id):
            if packages:
                break
            continue

        packages.append(
            Package(
                name=cells.get("name", ""),
                id=package_id,
                version=cells.get("version", ""),
                available=cells.get("available", ""),
                match=cells.get("match", ""),
                source=cells.get("source", ""),
            )
        )
        if len(packages) >= limit:
            break
    return packages


def _tail(output: str, limit: int = 12) -> str:
    """Últimas linhas relevantes da saída, sem lixo de progresso."""
    useful: list[str] = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if re.fullmatch(r"[\d.,]+\s*(B|KB|MB|GB|TB)(\s*/\s*[\d.,]+\s*(B|KB|MB|GB|TB))?", stripped, re.I):
            continue
        if set(stripped) <= set("█▒░■□─-=|/\\ "):
            continue
        useful.append(stripped)
    return "\n".join(useful[-limit:])


class Winget:
    """Fachada para os comandos do winget usados pelo WPM."""

    def __init__(self, config: Config, console: Console) -> None:
        self.config = config
        self.console = console
        self.executable: Path | None = find_winget()
        self._version: str | None = None

    def require(self) -> str:
        """Garante que o winget está disponível e devolve seu caminho."""
        if self.executable is None:
            raise WingetNotFoundError(
                "winget não encontrado. Instale o 'App Installer' pela Microsoft Store."
            )
        return str(self.executable)

    def version(self) -> str:
        """Versão do winget (ex.: '1.29.380')."""
        if self._version is not None:
            return self._version
        if self.executable is None:
            self._version = ""
            return self._version
        result = run_capture([str(self.executable), "--version"], timeout=20)
        if result.succeeded:
            first = result.output.strip().splitlines()[0] if result.output.strip() else ""
            match = re.search(r"(\d+(?:\.\d+)+)", first)
            self._version = match.group(1) if match else first
        else:
            self._version = ""
        return self._version

    def version_tuple(self) -> tuple[int, ...]:
        version = self.version()
        if not version:
            return ()
        return tuple(int(part) for part in re.findall(r"\d+", version))

    def _source_args(self, source: str | None = None) -> list[str]:
        chosen = source or self.config.source
        if not chosen or chosen.lower() in ("auto", "todas", "all"):
            return []
        return ["--source", chosen]

    def _common(self) -> list[str]:
        return ["--accept-source-agreements", "--disable-interactivity"]

    def _run(self, args: list[str], *, timeout: float | None, action: str) -> CommandResult:
        LOGGER.debug("executando: %s", " ".join(args))
        result = run_capture(args, timeout=timeout)
        LOGGER.debug("'%s' rc=%s em %.1fs", action, result.returncode, result.duration)
        if result.output:
            LOGGER.debug("saida de '%s':\n%s", action, result.output)
        return result

    def _raise_if_error(self, result: CommandResult, action: str) -> None:
        if result.timed_out:
            raise NetworkError(f"Tempo limite excedido ao {action}.")
        if result.succeeded:
            return
        if result.unsigned_code in (0x8A150014, 0x8A15002B):
            return
        text = result.output.casefold()
        if any(marker in text for marker in _NO_RESULTS_MARKERS):
            return
        if any(marker in text for marker in _NETWORK_MARKERS):
            raise NetworkError(f"Falha de conexão ao {action}. Detalhes:\n{_tail(result.output)}")
        code = _ERROR_CODES.get(result.unsigned_code)
        detail = _tail(result.output)
        if code:
            raise OperationFailedError(f"{code}\n{detail}".strip())
        raise OperationFailedError(
            f"O winget falhou ao {action} (código 0x{result.unsigned_code:08X}).\n{detail}".strip()
        )

    def search(self, query: str, *, limit: int | None = None, source: str | None = None) -> list[Package]:
        """Pesquisa pacotes nas fontes configuradas."""
        query = query.strip()
        if not query:
            raise OperationFailedError("Informe um termo de busca.")
        executable = self.require()
        args = [
            executable,
            "search",
            "--query",
            query,
            *self._common(),
            *self._source_args(source),
        ]
        result = self._run(args, timeout=min(self.config.timeout, 180), action="pesquisar")
        self._raise_if_error(result, "pesquisar")
        return parse_table(result.output, limit=limit or self.config.search_limit)

    def search_by_id(self, package_id: str, *, source: str | None = None) -> list[Package]:
        """Pesquisa exata por ID de pacote."""
        package_id = package_id.strip()
        if not package_id:
            return []
        executable = self.require()
        args = [
            executable,
            "search",
            "--id",
            package_id,
            "--exact",
            *self._common(),
            *self._source_args(source),
        ]
        result = self._run(args, timeout=min(self.config.timeout, 180), action="pesquisar por ID")
        self._raise_if_error(result, "pesquisar por ID")
        return parse_table(result.output, limit=50)

    def list_installed(self) -> list[Package]:
        """Lista pacotes instalados no sistema."""
        executable = self.require()
        args = [executable, "list", *self._common(), *self._source_args()]
        result = self._run(args, timeout=min(self.config.timeout, 240), action="listar instalados")
        self._raise_if_error(result, "listar instalados")
        return parse_table(result.output, limit=10000)

    def list_upgradable(self, include_unknown: bool = True) -> list[Package]:
        """Lista pacotes com atualizações disponíveis."""
        executable = self.require()
        args = [executable, "upgrade", *self._common(), *self._source_args()]
        if include_unknown:
            args.append("--include-unknown")
        result = self._run(args, timeout=min(self.config.timeout, 240), action="verificar atualizações")
        self._raise_if_error(result, "verificar atualizações")
        return parse_table(result.output, limit=10000)

    def source_update(self) -> CommandResult:
        """Atualiza os índices das fontes configuradas."""
        executable = self.require()
        args = [executable, "source", "update", "--disable-interactivity"]
        result = self._run(args, timeout=min(self.config.timeout, 180), action="atualizar fontes")
        return result

    def source_list(self) -> CommandResult:
        """Obtém a lista de fontes do winget."""
        executable = self.require()
        args = [executable, "source", "list"]
        return self._run(args, timeout=min(self.config.timeout, 60), action="listar fontes")

    def install(
        self,
        package: Package,
        *,
        version: str | None = None,
        scope: str | None = None,
        location: str | None = None,
        silent: bool = True,
        interactive: bool = False,
    ) -> CommandResult:
        """Instala um pacote."""
        executable = self.require()
        args = [
            executable,
            "install",
            "--id",
            package.id,
            "--exact",
            "--accept-package-agreements",
            "--accept-source-agreements",
            *self._source_args(package.source or None),
        ]
        if version:
            args.extend(["--version", version])
        if scope:
            args.extend(["--scope", scope])
        if location:
            args.extend(["--location", location])
        if interactive:
            args.append("--interactive")
        elif silent:
            args.append("--silent")
        if not interactive:
            args.append("--disable-interactivity")
        return self._stream_action(args, "Baixando/instalando", track_downloads=True)

    def download(
        self,
        package: Package,
        directory: Path,
        *,
        version: str | None = None,
    ) -> CommandResult:
        """Baixa o instalador de um pacote para uma pasta local."""
        executable = self.require()
        args = [
            executable,
            "download",
            "--id",
            package.id,
            "--exact",
            "--download-directory",
            str(directory),
            "--accept-package-agreements",
            "--accept-source-agreements",
            "--disable-interactivity",
            *self._source_args(package.source or None),
        ]
        if version:
            args.extend(["--version", version])
        return self._stream_action(
            args,
            "Baixando",
            monitor_dir=directory,
            track_downloads=True,
        )

    def upgrade(
        self,
        package: Package | None = None,
        *,
        all_packages: bool = False,
        include_unknown: bool = False,
        silent: bool = True,
        interactive: bool = False,
    ) -> CommandResult:
        """Atualiza um pacote específico ou todos os pacotes."""
        executable = self.require()
        args = [
            executable,
            "upgrade",
            "--accept-package-agreements",
            "--accept-source-agreements",
            *self._source_args(package.source if package and package.source else None),
        ]
        if all_packages:
            args.append("--all")
            if include_unknown:
                args.append("--include-unknown")
        elif package is not None:
            args.extend(["--id", package.id, "--exact"])
            if include_unknown:
                args.append("--include-unknown")
        else:
            raise OperationFailedError("Informe um pacote ou use --all para atualizar todos.")
        if interactive:
            args.append("--interactive")
        elif silent:
            args.append("--silent")
        if not interactive:
            args.append("--disable-interactivity")
        label = "Atualizando" if package else "Atualizando todos"
        return self._stream_action(args, label, track_downloads=True)

    def uninstall(
        self,
        package: Package,
        *,
        silent: bool = True,
        interactive: bool = False,
    ) -> CommandResult:
        """Desinstala um pacote instalado."""
        executable = self.require()
        args = [executable, "uninstall", "--accept-source-agreements"]
        if "\\" in package.id or not package.source:
            args.extend(["--name", package.display_name, "--exact"])
        else:
            args.extend(["--id", package.id, "--exact"])
        if interactive:
            args.append("--interactive")
        elif silent:
            args.append("--silent")
        if not interactive:
            args.append("--disable-interactivity")
        return self._stream_action(args, "Desinstalando")

    def _stream_action(
        self,
        args: list[str],
        label: str,
        *,
        monitor_dir: Path | None = None,
        track_downloads: bool = False,
    ) -> CommandResult:
        LOGGER.debug("executando: %s", " ".join(args))
        progress = Progress(self.console, label)
        saw_output = False
        stop_monitor = threading.Event()
        monitor: threading.Thread | None = None
        monitor_paths: list[Path] = []
        baselines: dict[Path, int] = {}

        def on_text(text: str) -> None:
            nonlocal saw_output
            if text.strip():
                saw_output = True
                LOGGER.debug("winget> %s", truncate(text, 500))
            progress.feed(text)

        if monitor_dir is not None:
            monitor_paths.append(monitor_dir)
        if track_downloads:
            temp_root = os.environ.get("TEMP")
            if temp_root:
                monitor_paths.append(Path(temp_root) / "WinGet")
        if monitor_paths and progress.enabled:
            baselines = {path: _directory_total(path) for path in monitor_paths}

            def downloaded_total() -> int:
                return sum(
                    max(0, _directory_total(path) - baseline)
                    for path, baseline in baselines.items()
                )

            def watch() -> None:
                while not stop_monitor.wait(0.5):
                    progress.set_bytes(downloaded_total())

            monitor = threading.Thread(target=watch, name="wpm-download", daemon=True)
            monitor.start()

        try:
            result = stream_command(
                args,
                on_text,
                timeout=self.config.timeout,
            )
        finally:
            stop_monitor.set()
            if monitor is not None:
                monitor.join(timeout=1.0)
            if baselines:
                progress.set_bytes(
                    sum(
                        max(0, _directory_total(path) - baseline)
                        for path, baseline in baselines.items()
                    )
                )
            progress.finish()
        if not saw_output:
            LOGGER.debug("(sem saída do winget)")
        LOGGER.info("comando rc=%s em %.1fs: %s", result.returncode, result.duration, " ".join(args))
        return result

    @staticmethod
    def is_benign(result: CommandResult) -> bool:
        """Detecta falhas que na prática significam 'nada a fazer'."""
        if result.succeeded:
            return False
        text = result.output.casefold()
        return any(marker in text for marker in _BENIGN_MARKERS)

    @staticmethod
    def is_network_failure(result: CommandResult) -> bool:
        """Detecta falhas causadas por conectividade com as fontes."""
        if result.succeeded:
            return False
        text = result.output.casefold()
        return any(marker in text for marker in _NETWORK_MARKERS)

    @staticmethod
    def describe_failure(result: CommandResult, action: str = "executar a operação") -> str:
        """Mensagem amigável e detalhada para uma falha do winget."""
        if result.timed_out:
            return f"Tempo limite excedido ao {action}."
        code = _ERROR_CODES.get(result.unsigned_code)
        detail = _tail(result.output)
        if code:
            return f"{code}\n{detail}".strip()
        return (
            f"O winget falhou ao {action} (código 0x{result.unsigned_code:08X}).\n{detail}"
        ).strip()

    def network_check(self) -> tuple[bool, str]:
        """Testa rapidamente o acesso às fontes remotas."""
        try:
            result = run_capture(
                [
                    self.require(),
                    "search",
                    "--query",
                    "7zip",
                    "--accept-source-agreements",
                    "--disable-interactivity",
                ],
                timeout=40,
            )
        except WingetNotFoundError as exc:
            return False, str(exc)
        if result.succeeded:
            return True, "consulta às fontes concluída"
        if any(marker in result.output.casefold() for marker in _NO_RESULTS_MARKERS):
            return True, "fontes acessíveis (consulta sem resultados)"
        if result.timed_out:
            return False, "tempo limite excedido"
        return False, _tail(result.output, 3) or "falha desconhecida"
