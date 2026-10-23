"""Interface de linha de comando e menu interativo do WPM."""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import __version__
from .cleanup import delete_residual, scan_residuals
from .config import CONFIG_PATH, LOG_FILE, Config, configure_logging
from .errors import (
    ConfigurationError,
    NetworkError,
    OperationFailedError,
    PackageNotFoundError,
    UserCancelled,
    WingetNotFoundError,
    WpmError,
)
from .models import CommandResult, Package
from .platform_win import (
    enable_ansi,
    find_winget,
    free_space,
    is_admin,
    relaunch_elevated,
)
from .ui import Console, format_size
from .winget import Winget

LOGGER = logging.getLogger("wpm.cli")

EXAMPLES = """Exemplos:
  wpm search firefox                 Procura pacotes
  wpm install 7zip.7zip              Instala pelo ID
  wpm download "Google Chrome"       Baixa o instalador
  wpm list --upgradable              Lista atualizações
  wpm update --all                   Atualiza tudo
  wpm remove firefox --purge         Desinstala e limpa resíduos
  wpm clean "K-Lite" --dry-run       Simula a limpeza de resíduos
  wpm doctor                         Diagnóstico do ambiente
  wpm                                Abre o menu interativo
"""


@dataclass
class Context:
    config: Config
    console: Console
    winget: Winget
    assume_yes: bool = False
    verbose: bool = False
    raw_args: list[str] | None = None


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------


def _configure_stdio() -> None:
    """Evita UnicodeEncodeError quando a saída é redirecionada para arquivo."""
    for stream in (sys.stdout, sys.stderr):
        try:
            if hasattr(stream, "reconfigure"):
                if not stream.isatty():
                    stream.reconfigure(encoding="utf-8", errors="replace")
                else:
                    stream.reconfigure(errors="replace")
        except (OSError, ValueError):
            pass


def _build_console(args: argparse.Namespace, config: Config) -> Console:
    mode = "never" if getattr(args, "no_color", False) else config.color
    if mode == "always":
        enable_ansi()
        color = True
    elif mode == "never":
        color = False
    else:
        color = bool(getattr(sys.stdout, "isatty", lambda: False)()) and enable_ansi()
    return Console(color=color, verbose=getattr(args, "verbose", False))


def _confirm(ctx: Context, prompt: str, default: bool = False) -> None:
    if ctx.assume_yes:
        return
    if not ctx.console.is_tty:
        raise UserCancelled("Confirmação necessária em modo não interativo. Use --yes.")
    if not ctx.console.confirm(prompt, default=default):
        raise UserCancelled("Operação cancelada.")


def _preview(args: list[str]) -> str:
    return subprocess.list2cmdline(args)


def _limit_value(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("o limite deve ser um número inteiro") from exc
    if not 1 <= value <= 200:
        raise argparse.ArgumentTypeError("o limite deve estar entre 1 e 200")
    return value


def _absolute(value: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(value))).resolve()


def _project_cwd() -> str:
    candidate = Path(__file__).resolve().parent.parent
    if (candidate / "wpm" / "__init__.py").is_file():
        return str(candidate)
    return str(Path.cwd())


def _maybe_elevate_at_start(ctx: Context, requested: bool) -> int | None:
    """Relança o comando via UAC antes de qualquer trabalho interativo."""
    if not requested or is_admin():
        return None
    if not ctx.raw_args:
        ctx.console.warn(
            "Não é possível elevar o menu interativo. Execute o comando com --elevate."
        )
        return None
    ctx.console.info("Solicitando privilégios de administrador (UAC)...")
    code = relaunch_elevated(["-m", "wpm", *ctx.raw_args], cwd=_project_cwd())
    if code == 0:
        ctx.console.success("Operação concluída na janela elevada.")
    else:
        ctx.console.warn(f"A janela elevada terminou com código {code}.")
    return code


def _print_package(ctx: Context, package: Package) -> None:
    console = ctx.console
    console.print(f"  Nome:     {package.display_name}")
    console.print(f"  ID:       {package.id}")
    console.print(f"  Versão:   {package.version or 'desconhecida'}")
    if package.available:
        console.print(f"  Disponível: {package.available}")
    console.print(f"  Fonte:    {package.source or 'local/desconhecida'}")


def _filter_packages(packages: list[Package], query: str) -> list[Package]:
    terms = [term for term in query.casefold().split() if term]
    if not terms:
        return packages

    def matches(package: Package) -> bool:
        haystack = f"{package.name} {package.id}".casefold()
        return all(term in haystack for term in terms)

    return [package for package in packages if matches(package)]


def _resolve_remote(
    ctx: Context,
    query: str,
    *,
    exact_id: bool = False,
    source: str | None = None,
) -> Package:
    """Resolve um termo de busca em um único pacote remoto."""
    if exact_id:
        results = ctx.winget.search_by_id(query, source=source)
    else:
        results = ctx.winget.search(query, source=source)

    if not results:
        raise PackageNotFoundError(f"Nenhum pacote encontrado para '{query}'.")

    exact = [package for package in results if package.id.casefold() == query.casefold()]
    if exact:
        return exact[0]
    if len(results) == 1:
        return results[0]

    ctx.console.title(f"Resultados para '{query}'")
    ctx.console.packages_table(results)
    index = ctx.console.choose("Qual pacote deseja usar?", len(results))
    if index is None:
        raise UserCancelled("Nenhum pacote selecionado.")
    return results[index]


def _resolve_installed(ctx: Context, query: str, installed: list[Package]) -> Package:
    """Resolve um termo contra a lista de pacotes instalados."""
    lowered = query.casefold().strip()
    matches = [package for package in installed if package.id.casefold() == lowered]
    if not matches:
        matches = [package for package in installed if lowered in package.id.casefold()]
    if not matches:
        matches = [package for package in installed if lowered in package.name.casefold()]
    if not matches:
        raise PackageNotFoundError(f"'{query}' não parece estar instalado.")

    if len(matches) == 1:
        return matches[0]
    ctx.console.title(f"Pacotes instalados correspondentes a '{query}'")
    ctx.console.packages_table(matches)
    index = ctx.console.choose("Qual pacote deseja usar?", len(matches))
    if index is None:
        raise UserCancelled("Nenhum pacote selecionado.")
    return matches[index]


def _handle_action_result(
    ctx: Context,
    result: CommandResult,
    package: Package,
    verb: str,
) -> int:
    if result.succeeded:
        ctx.console.success(f"{package.display_name}: {verb} com êxito.")
        return 0
    if ctx.winget.is_benign(result):
        ctx.console.info(f"{package.display_name}: nenhuma alteração necessária.")
        return 0
    detail = ctx.winget.describe_failure(result, verb)
    if ctx.winget.is_network_failure(result):
        raise NetworkError(detail)
    raise OperationFailedError(detail)


# ---------------------------------------------------------------------------
# Serviços reutilizados pela CLI e pelo menu
# ---------------------------------------------------------------------------


def service_install(
    ctx: Context,
    package: Package,
    *,
    version: str | None = None,
    scope: str | None = None,
    location: str | None = None,
    interactive: bool = False,
    dry_run: bool = False,
) -> int:
    console = ctx.console
    console.title("Instalar pacote")
    _print_package(ctx, package)
    if scope:
        console.print(f"  Escopo:   {scope}")
    if location:
        console.print(f"  Destino:  {location}")
    console.print(
        f"  Modo:     {'interativo' if interactive else ('silencioso' if ctx.config.silent_install else 'padrão')}"
    )

    preview = ["winget", "install", "--id", package.id, "--exact"]
    if version:
        preview += ["--version", version]
    if scope:
        preview += ["--scope", scope]
    if location:
        preview += ["--location", location]
    if interactive:
        preview.append("--interactive")
    elif ctx.config.silent_install:
        preview.append("--silent")

    if dry_run:
        console.info(f"Simulação: {_preview(preview)}")
        return 0
    _confirm(ctx, f"Instalar '{package.display_name}' agora?", default=True)
    if ctx.verbose:
        console.hint(_preview(preview))

    result = ctx.winget.install(
        package,
        version=version,
        scope=scope,
        location=location,
        silent=not interactive and ctx.config.silent_install,
        interactive=interactive,
    )
    return _handle_action_result(ctx, result, package, "instalado")


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _detect_downloaded_files(directory: Path, before: set[Path], started: float) -> list[Path]:
    try:
        current = {item.resolve() for item in directory.iterdir() if item.is_file()}
    except OSError:
        return []
    new_files = sorted(current - before)
    if new_files:
        return new_files

    suffixes = {".exe", ".msi", ".msix", ".appx", ".msixbundle", ".zip", ".7z"}
    recent: list[Path] = []
    for item in current:
        try:
            if item.suffix.casefold() in suffixes and item.stat().st_mtime >= started - 10:
                recent.append(item)
        except OSError:
            continue
    return sorted(recent)


def _write_checksums(package: Package, files: list[Path], directory: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_id = "".join(char if char.isalnum() or char in "._-" else "_" for char in package.id)
    checksum_file = directory / f"{safe_id}-{stamp}.sha256"
    lines = [f"{_hash_file(path)}  {path.name}" for path in files]
    checksum_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return checksum_file


def service_download(
    ctx: Context,
    package: Package,
    *,
    version: str | None = None,
    directory: Path | None = None,
    dry_run: bool = False,
) -> int:
    console = ctx.console
    target = directory or ctx.config.download_path
    console.title("Baixar instalador")
    _print_package(ctx, package)
    console.print(f"  Pasta:    {target}")

    preview = ["winget", "download", "--id", package.id, "--exact", "-d", str(target)]
    if version:
        preview += ["--version", version]

    if dry_run:
        console.info(f"Simulação: {_preview(preview)}")
        return 0

    _confirm(ctx, f"Baixar o instalador de '{package.display_name}'?", default=True)

    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OperationFailedError(f"Não foi possível criar a pasta '{target}': {exc}") from exc

    available = free_space(target)
    if available and available < 500 * 1024 * 1024:
        console.warn(f"Pouco espaço livre em disco: {format_size(available)}.")
    if not os.access(target, os.W_OK):
        raise OperationFailedError(f"Sem permissão de escrita em '{target}'.")

    try:
        before = {item.resolve() for item in target.iterdir() if item.is_file()}
    except OSError:
        before = set()
    started = time.time()

    result = ctx.winget.download(package, target, version=version)
    if not result.succeeded:
        if ctx.winget.is_benign(result):
            console.info("O download não era necessário.")
            return 0
        detail = ctx.winget.describe_failure(result, "baixar o instalador")
        if ctx.winget.is_network_failure(result):
            raise NetworkError(detail)
        raise OperationFailedError(detail)

    files = _detect_downloaded_files(target, before, started)
    if not files:
        console.warn("O winget informou sucesso, mas nenhum arquivo novo foi identificado.")
        console.info(f"Verifique manualmente a pasta: {target}")
        return 0

    console.success(f"Download concluído em {target}")
    for path in files:
        try:
            size = format_size(path.stat().st_size)
        except OSError:
            size = "?"
        console.print(f"  Arquivo: {path.name} ({size})")
        if ctx.config.checksum_after_download:
            try:
                console.print(f"  SHA-256: {_hash_file(path)}", "grey")
            except OSError as exc:
                console.warn(f"Não foi possível calcular o hash de {path.name}: {exc}")

    if ctx.config.checksum_after_download:
        try:
            checksum_file = _write_checksums(package, files, target)
            console.info(f"Checksums salvos em {checksum_file}")
        except OSError as exc:
            console.warn(f"Não foi possível salvar o arquivo de checksums: {exc}")
    return 0


def service_list(ctx: Context, *, upgradable: bool = False, text_filter: str = "") -> int:
    packages = ctx.winget.list_upgradable() if upgradable else ctx.winget.list_installed()
    if text_filter:
        packages = _filter_packages(packages, text_filter)
    if not packages:
        ctx.console.info(
            "Nenhuma atualização disponível." if upgradable else "Nenhum pacote encontrado."
        )
        return 0
    ctx.console.title(
        "Atualizações disponíveis" if upgradable else "Pacotes instalados"
    )
    ctx.console.packages_table(packages, show_available=upgradable or any(p.available for p in packages))
    ctx.console.hint(f"{len(packages)} pacote(s).")
    return 0


def _prepare_upgrades(ctx: Context, *, update_sources: bool = True) -> list[Package]:
    if ctx.config.update_sources and update_sources:
        ctx.console.info("Atualizando índices das fontes do winget...")
        result = ctx.winget.source_update()
        if not result.succeeded:
            ctx.console.warn("Não foi possível atualizar os índices; usando o cache local.")
    return ctx.winget.list_upgradable(include_unknown=True)


def service_upgrade(
    ctx: Context,
    packages: list[Package],
    *,
    include_unknown: bool = True,
    interactive: bool = False,
    dry_run: bool = False,
) -> int:
    console = ctx.console
    if not packages:
        console.success("Nenhuma atualização disponível.")
        return 0

    failures: list[tuple[Package, str]] = []
    successes = 0
    benign = 0
    for index, package in enumerate(packages, start=1):
        console.print()
        console.print(
            f"[{index}/{len(packages)}] {package.display_name}: "
            f"{package.version or '?'} -> {package.available or '?'}",
            "bold",
        )
        if dry_run:
            mode = "--interactive" if interactive else (
                "--silent" if ctx.config.silent_install else ""
            )
            suffix = f" {mode}" if mode else ""
            console.info(f"Simulação: winget upgrade --id {package.id} --exact{suffix}")
            continue
        result = ctx.winget.upgrade(
            package,
            include_unknown=include_unknown,
            silent=ctx.config.silent_install and not interactive,
            interactive=interactive,
        )
        if result.succeeded:
            successes += 1
            console.success(f"{package.display_name} atualizado.")
        elif ctx.winget.is_benign(result):
            benign += 1
            console.info(f"{package.display_name} já está atualizado.")
        else:
            detail = ctx.winget.describe_failure(result, "atualizar")
            failures.append((package, detail))
            console.error(f"Falha ao atualizar {package.display_name}.")

    console.print()
    if dry_run:
        console.info(f"Simulação concluída para {len(packages)} pacote(s).")
        return 0
    if failures:
        console.warn(f"{successes} atualizado(s), {benign} sem alteração, {len(failures)} falha(s).")
        for package, detail in failures:
            console.error(f"{package.display_name}: {detail.splitlines()[0]}")
        console.hint("Detalhes completos no log. Tente --interactive para instaladores problemáticos.")
        return 7
    console.success(f"Atualização concluída: {successes} pacote(s), {benign} sem alteração.")
    return 0


def _cleanup_flow(
    ctx: Context,
    name: str,
    package_id: str,
    *,
    include_system: bool = True,
    dry_run: bool = False,
    permanent: bool = False,
    auto_all: bool = False,
) -> int:
    console = ctx.console
    console.title("Limpeza de resíduos")
    console.info(f"Procurando pastas relacionadas a '{name}'...")
    residuals = scan_residuals(name, package_id, include_system=include_system)
    if not residuals:
        console.success("Nenhum resíduo encontrado nas pastas verificadas.")
        return 0

    rows = []
    for residual in residuals:
        location = ", ".join(residual.tags) or "?"
        if residual.requires_admin:
            location += " (admin)"
        rows.append(
            [
                str(residual.path),
                format_size(residual.size),
                residual.reason,
                location,
            ]
        )
    console.table(
        ["Pasta", "Tamanho", "Motivo", "Local"],
        rows,
        index=True,
        max_widths=[48, 10, 28, 22],
    )
    if dry_run:
        console.info("Simulação: nenhum arquivo foi removido.")
        return 0

    if auto_all:
        selected = [index for index, residual in enumerate(residuals) if residual.exact]
        skipped = len(residuals) - len(selected)
        if skipped:
            console.warn(
                f"{skipped} item(ns) sem correspondência exata não serão removidos automaticamente."
            )
        if not selected:
            console.info("Nenhum item com correspondência exata para remover automaticamente.")
            return 0
    else:
        selected = console.select_many("Selecione os itens para remover", len(residuals))
        if not selected:
            console.info("Nenhum item selecionado.")
            return 0

    total = sum((residuals[index].size or 0) for index in selected)
    requires_admin = any(residuals[index].requires_admin for index in selected)
    if requires_admin and not is_admin():
        console.warn("Alguns itens exigem privilégios de administrador; use --elevate.")

    action = "excluir permanentemente" if permanent else "mover para a Lixeira"
    _confirm(ctx, f"{action} {len(selected)} item(ns) ({format_size(total)})?", default=False)
    if permanent and console.is_tty:
        answer = console.ask('Digite "EXCLUIR" para confirmar a remoção permanente', required=True)
        if answer != "EXCLUIR":
            raise UserCancelled("Confirmação inválida; nada foi removido.")

    failures = 0
    for index in selected:
        residual = residuals[index]
        try:
            delete_residual(residual, permanent=permanent)
            console.success(f"Removido: {residual.path}")
        except WpmError as exc:
            failures += 1
            console.error(str(exc))
    if failures:
        console.warn(f"{failures} item(ns) não puderam ser removidos.")
        if not is_admin():
            console.hint("Tente novamente com --elevate.")
        return 7
    console.success(f"Limpeza concluída: {len(selected)} item(ns) processado(s).")
    return 0


def service_remove(
    ctx: Context,
    package: Package,
    *,
    interactive: bool = False,
    purge: bool = False,
    permanent: bool = False,
    include_system: bool = True,
    dry_run: bool = False,
) -> int:
    console = ctx.console
    console.title("Remover pacote")
    _print_package(ctx, package)
    preview = ["winget", "uninstall"]
    if "\\" in package.id or not package.source:
        preview += ["--name", package.display_name, "--exact"]
    else:
        preview += ["--id", package.id, "--exact"]
    if interactive:
        preview.append("--interactive")
    elif ctx.config.silent_install:
        preview.append("--silent")

    if dry_run:
        console.info(f"Simulação: {_preview(preview)}")
        if purge:
            return _cleanup_flow(
                ctx,
                package.display_name,
                package.id,
                include_system=include_system,
                dry_run=True,
                permanent=permanent,
            )
        return 0

    _confirm(ctx, f"Desinstalar '{package.display_name}'?", default=False)
    result = ctx.winget.uninstall(
        package,
        silent=ctx.config.silent_install and not interactive,
        interactive=interactive,
    )
    status = _handle_action_result(ctx, result, package, "desinstalado")
    if status != 0:
        return status

    if purge:
        console.print()
        return _cleanup_flow(
            ctx,
            package.display_name,
            package.id,
            include_system=include_system,
            permanent=permanent,
            auto_all=ctx.assume_yes,
        )

    if console.is_tty and not ctx.assume_yes:
        if console.confirm("Procurar e remover dados residuais do aplicativo?", default=False):
            return _cleanup_flow(
                ctx,
                package.display_name,
                package.id,
                include_system=include_system,
                permanent=permanent,
            )
    return 0


def service_doctor(ctx: Context) -> int:
    console = ctx.console
    console.title("Diagnóstico do ambiente")
    checks: list[list[str]] = []

    python_ok = sys.version_info >= (3, 9)
    checks.append(
        ["Python", "OK" if python_ok else "FALHA", f"{sys.version.split()[0]} (requer >= 3.9)"]
    )

    winget_path = find_winget()
    checks.append(["winget", "OK" if winget_path else "FALHA", str(winget_path or "não encontrado")])
    if winget_path:
        version = ctx.winget.version() or "?"
        version_tuple = ctx.winget.version_tuple()
        download_ok = version_tuple >= (1, 6, 0)
        checks.append(
            [
                "Versão do winget",
                "OK" if download_ok else "AVISO",
                f"{version} (download requer >= 1.6.0)",
            ]
        )
        checks.append(["Administrador", "INFO", "sim" if is_admin() else "não (UAC pode ser necessário)"])
        network_ok, network_detail = ctx.winget.network_check()
        checks.append(["Rede/fontes", "OK" if network_ok else "FALHA", network_detail])
    else:
        checks.append(["Administrador", "INFO", "sim" if is_admin() else "não"])

    checks.append(["Configuração", "INFO", str(CONFIG_PATH)])
    checks.append(["Logs", "INFO", str(LOG_FILE)])
    checks.append(["Pasta de download", "INFO", str(ctx.config.download_path)])

    download_dir = ctx.config.download_path
    if download_dir.exists():
        checks.append(["Espaço livre", "INFO", format_size(free_space(download_dir))])
    else:
        checks.append(["Espaço livre", "INFO", "pasta de download ainda não existe"])

    console.table(["Verificação", "Status", "Detalhe"], checks, max_widths=[22, 10, 62])
    console.print()

    if winget_path:
        sources = ctx.winget.source_list()
        if sources.succeeded and sources.output.strip():
            console.title("Fontes configuradas")
            for line in sources.output.strip().splitlines()[:12]:
                if line.strip():
                    console.print(f"  {line.rstrip()}")

    if not winget_path:
        console.error("Instale o 'App Installer' pela Microsoft Store para usar o WPM.")
        return 3
    return 0


# ---------------------------------------------------------------------------
# Handlers dos subcomandos
# ---------------------------------------------------------------------------


def cmd_search(ctx: Context, args: argparse.Namespace) -> int:
    query = " ".join(args.query)
    packages = ctx.winget.search(query, limit=args.limit, source=args.source)
    if not packages:
        ctx.console.warn(f"Nenhum pacote encontrado para '{query}'.")
        return 0

    ctx.console.title(f"Resultados para '{query}'")
    ctx.console.packages_table(packages, show_available=False)
    ctx.console.hint(f"{len(packages)} resultado(s). Use 'wpm install <ID>' para instalar.")
    if not args.install:
        return 0

    index = ctx.console.choose("Instalar qual pacote?", len(packages))
    if index is None:
        ctx.console.info("Nenhum pacote selecionado.")
        return 0
    return service_install(ctx, packages[index])


def cmd_install(ctx: Context, args: argparse.Namespace) -> int:
    elevated = _maybe_elevate_at_start(ctx, args.elevate and not args.dry_run)
    if elevated is not None:
        return elevated
    query = " ".join(args.package)
    package = _resolve_remote(
        ctx,
        query,
        exact_id=args.id,
        source=args.source,
    )
    location = str(_absolute(args.location)) if args.location else None
    return service_install(
        ctx,
        package,
        version=args.version,
        scope=args.scope,
        location=location,
        interactive=args.interactive,
        dry_run=args.dry_run,
    )


def cmd_download(ctx: Context, args: argparse.Namespace) -> int:
    query = " ".join(args.package)
    package = _resolve_remote(ctx, query, exact_id=args.id, source=args.source)
    directory = _absolute(args.dir) if args.dir else ctx.config.download_path
    return service_download(
        ctx,
        package,
        version=args.version,
        directory=directory,
        dry_run=args.dry_run,
    )


def cmd_list(ctx: Context, args: argparse.Namespace) -> int:
    text_filter = " ".join(args.filter)
    return service_list(ctx, upgradable=args.upgradable, text_filter=text_filter)


def cmd_update(ctx: Context, args: argparse.Namespace) -> int:
    elevated = _maybe_elevate_at_start(ctx, args.elevate and not args.dry_run)
    if elevated is not None:
        return elevated

    packages = _prepare_upgrades(ctx, update_sources=not args.dry_run)
    if not packages:
        ctx.console.success("Tudo atualizado. Nenhuma atualização disponível.")
        return 0

    ctx.console.title("Atualizações disponíveis")
    ctx.console.packages_table(packages)

    targets: list[Package] = []
    if args.all:
        targets = packages
        if not args.include_unknown:
            ignored = [
                package
                for package in packages
                if not package.version
                or "desconhecid" in package.version.casefold()
                or "unknown" in package.version.casefold()
            ]
            if ignored:
                ctx.console.warn(
                    f"{len(ignored)} pacote(s) com versão desconhecida serão ignorados "
                    "(use --include-unknown para incluí-los)."
                )
                ignored_ids = {package.id for package in ignored}
                targets = [package for package in packages if package.id not in ignored_ids]
    elif args.package:
        seen: set[str] = set()
        for query in args.package:
            package = _resolve_installed_upgrade(ctx, query, packages)
            if package.id not in seen:
                seen.add(package.id)
                targets.append(package)
    else:
        if not ctx.console.is_tty and not ctx.assume_yes:
            raise UserCancelled("Informe os pacotes ou use --all em modo não interativo.")
        selected = ctx.console.select_many("Atualizar quais pacotes?", len(packages))
        if not selected:
            ctx.console.info("Nenhum pacote selecionado.")
            return 0
        targets = [packages[index] for index in selected]

    if not targets:
        ctx.console.info("Nenhum pacote selecionado.")
        return 0

    if not args.dry_run:
        _confirm(ctx, f"Atualizar {len(targets)} pacote(s)?", default=True)
    return service_upgrade(
        ctx,
        targets,
        include_unknown=args.include_unknown or bool(args.package),
        interactive=args.interactive,
        dry_run=args.dry_run,
    )


def _resolve_installed_upgrade(ctx: Context, query: str, upgrades: list[Package]) -> Package:
    lowered = query.casefold().strip()
    matches = [package for package in upgrades if package.id.casefold() == lowered]
    if not matches:
        matches = [package for package in upgrades if lowered in package.id.casefold()]
    if not matches:
        matches = [package for package in upgrades if lowered in package.name.casefold()]
    if not matches:
        installed = ctx.winget.list_installed()
        installed_matches = _filter_packages(installed, query)
        if installed_matches:
            names = ", ".join(package.display_name for package in installed_matches[:3])
            raise PackageNotFoundError(
                f"Nenhuma atualização disponível para '{query}' (instalado: {names})."
            )
        raise PackageNotFoundError(f"'{query}' não está instalado ou não possui atualização.")
    if len(matches) == 1:
        return matches[0]
    ctx.console.title(f"Atualizações correspondentes a '{query}'")
    ctx.console.packages_table(matches)
    index = ctx.console.choose("Qual pacote atualizar?", len(matches))
    if index is None:
        raise UserCancelled("Nenhum pacote selecionado.")
    return matches[index]


def cmd_remove(ctx: Context, args: argparse.Namespace) -> int:
    elevated = _maybe_elevate_at_start(ctx, args.elevate and not args.dry_run)
    if elevated is not None:
        return elevated

    installed = ctx.winget.list_installed()
    if not installed:
        ctx.console.warn("Nenhum pacote instalado foi encontrado.")
        return 0

    if args.package:
        seen: set[str] = set()
        targets: list[Package] = []
        for query in args.package:
            package = _resolve_installed(ctx, query, installed)
            if package.id not in seen:
                seen.add(package.id)
                targets.append(package)
    else:
        if not ctx.console.is_tty:
            raise UserCancelled("Informe o nome/ID do pacote a remover.")
        ctx.console.title("Pacotes instalados")
        ctx.console.packages_table(installed)
        index = ctx.console.choose("Qual pacote remover?", len(installed))
        if index is None:
            ctx.console.info("Nenhum pacote selecionado.")
            return 0
        targets = [installed[index]]

    status = 0
    for package in targets:
        result = service_remove(
            ctx,
            package,
            interactive=args.interactive,
            purge=args.purge,
            permanent=args.permanent,
            include_system=not args.no_system,
            dry_run=args.dry_run,
        )
        if result != 0:
            status = result
    return status


def cmd_clean(ctx: Context, args: argparse.Namespace) -> int:
    elevated = _maybe_elevate_at_start(ctx, args.elevate and not args.dry_run)
    if elevated is not None:
        return elevated

    name = " ".join(args.name).strip()
    if not name:
        if not ctx.console.is_tty:
            raise UserCancelled("Informe o nome ou ID do aplicativo.")
        name = ctx.console.ask("Nome ou ID do aplicativo", required=True)

    package_id = name if "." in name and " " not in name else ""
    return _cleanup_flow(
        ctx,
        name,
        package_id,
        include_system=not args.no_system,
        dry_run=args.dry_run,
        permanent=args.permanent,
        auto_all=ctx.assume_yes,
    )


def cmd_doctor(ctx: Context, args: argparse.Namespace) -> int:
    return service_doctor(ctx)


def _coerce_config_value(key: str, raw: str) -> object:
    if key in ("silent_install", "checksum_after_download", "update_sources"):
        lowered = raw.strip().casefold()
        if lowered in ("1", "true", "sim", "s", "yes", "y", "on"):
            return True
        if lowered in ("0", "false", "nao", "não", "n", "no", "off"):
            return False
        raise ConfigurationError(f"Valor booleano inválido para '{key}': {raw}")
    if key in ("timeout", "search_limit"):
        try:
            return int(raw)
        except ValueError as exc:
            raise ConfigurationError(f"Valor inteiro inválido para '{key}': {raw}") from exc
    return raw


def cmd_config(ctx: Context, args: argparse.Namespace) -> int:
    console = ctx.console
    config = ctx.config

    if args.reset:
        _confirm(ctx, "Restaurar todas as configurações padrão?", default=False)
        config = Config()
        config.save()
        console.success(f"Configuração restaurada em {CONFIG_PATH}")
        return 0

    changed = False
    for item in args.set or []:
        if "=" not in item:
            raise ConfigurationError("Use o formato chave=valor em --set.")
        key, raw = item.split("=", 1)
        key = key.strip()
        if key not in config.to_public_dict():
            raise ConfigurationError(f"Chave desconhecida: {key}")
        config.set(key, _coerce_config_value(key, raw.strip()))
        changed = True
        console.success(f"{key} = {getattr(config, key)}")

    if changed:
        config.save()
        console.info(f"Salvo em {CONFIG_PATH}")
        return 0

    if args.get:
        if args.get not in config.to_public_dict():
            raise ConfigurationError(f"Chave desconhecida: {args.get}")
        console.print(str(getattr(config, args.get)))
        return 0

    console.title("Configuração atual")
    console.table(
        ["Chave", "Valor"],
        [[key, str(value)] for key, value in config.to_public_dict().items()],
        max_widths=[26, 60],
    )
    console.print()
    console.print(f"  Arquivo: {CONFIG_PATH}", "grey")
    console.print(f"  Logs:    {LOG_FILE}", "grey")
    console.hint("Altere com: wpm config --set chave=valor")
    return 0


def cmd_version(ctx: Context, args: argparse.Namespace) -> int:
    winget_version = ctx.winget.version() if ctx.winget.executable else "não encontrado"
    ctx.console.print(f"wpm {__version__}")
    ctx.console.print(f"winget {winget_version}")
    ctx.console.print(f"Python {sys.version.split()[0]}")
    return 0


def cmd_menu(ctx: Context, args: argparse.Namespace) -> int:
    return interactive_menu(ctx)


HANDLERS = {
    "search": cmd_search,
    "find": cmd_search,
    "install": cmd_install,
    "add": cmd_install,
    "download": cmd_download,
    "fetch": cmd_download,
    "list": cmd_list,
    "ls": cmd_list,
    "update": cmd_update,
    "upgrade": cmd_update,
    "remove": cmd_remove,
    "uninstall": cmd_remove,
    "rm": cmd_remove,
    "clean": cmd_clean,
    "purge": cmd_clean,
    "doctor": cmd_doctor,
    "check": cmd_doctor,
    "config": cmd_config,
    "version": cmd_version,
    "menu": cmd_menu,
}


# ---------------------------------------------------------------------------
# Menu interativo
# ---------------------------------------------------------------------------


def _menu_search(ctx: Context) -> None:
    query = ctx.console.ask("Termo de busca", required=True)
    packages = ctx.winget.search(query)
    if not packages:
        ctx.console.warn("Nenhum pacote encontrado.")
        return
    ctx.console.title(f"Resultados para '{query}'")
    ctx.console.packages_table(packages, show_available=False)
    index = ctx.console.choose("Instalar algum pacote?", len(packages))
    if index is None:
        return
    service_install(ctx, packages[index])


def _menu_download(ctx: Context) -> None:
    query = ctx.console.ask("Nome ou ID do pacote", required=True)
    package = _resolve_remote(ctx, query)
    directory_text = ctx.console.ask(
        "Pasta de download",
        default=str(ctx.config.download_path),
    )
    service_download(ctx, package, directory=_absolute(directory_text))


def _menu_update(ctx: Context) -> None:
    packages = _prepare_upgrades(ctx)
    if not packages:
        ctx.console.success("Tudo atualizado.")
        return
    ctx.console.title("Atualizações disponíveis")
    ctx.console.packages_table(packages)
    selected = ctx.console.select_many("Atualizar quais pacotes?", len(packages))
    if not selected:
        ctx.console.info("Nenhum pacote selecionado.")
        return
    targets = [packages[index] for index in selected]
    _confirm(ctx, f"Atualizar {len(targets)} pacote(s)?", default=True)
    service_upgrade(ctx, targets, include_unknown=True)


def _menu_remove(ctx: Context) -> None:
    text_filter = ctx.console.ask("Filtrar pacotes instalados (Enter para todos)")
    installed = ctx.winget.list_installed()
    if text_filter:
        installed = _filter_packages(installed, text_filter)
    if not installed:
        ctx.console.warn("Nenhum pacote instalado encontrado.")
        return
    ctx.console.title("Pacotes instalados")
    ctx.console.packages_table(installed)
    index = ctx.console.choose("Qual pacote remover?", len(installed))
    if index is None:
        return
    purge = ctx.console.confirm("Procurar e remover resíduos após desinstalar?", default=False)
    service_remove(ctx, installed[index], purge=purge)


def _menu_list(ctx: Context) -> None:
    ctx.console.print("  1. Instalados")
    ctx.console.print("  2. Atualizações disponíveis")
    choice = ctx.console.ask("Opção", default="1")
    text_filter = ctx.console.ask("Filtrar (Enter para todos)")
    service_list(ctx, upgradable=choice.strip() == "2", text_filter=text_filter)


def _menu_clean(ctx: Context) -> None:
    name = ctx.console.ask("Nome ou ID do aplicativo", required=True)
    package_id = name if "." in name and " " not in name else ""
    _cleanup_flow(ctx, name, package_id, auto_all=False)


def _menu_doctor(ctx: Context) -> None:
    service_doctor(ctx)


def _menu_config(ctx: Context) -> None:
    console = ctx.console
    console.title("Configuração atual")
    console.table(
        ["Chave", "Valor"],
        [[key, str(value)] for key, value in ctx.config.to_public_dict().items()],
        max_widths=[26, 60],
    )
    console.print()
    console.print(f"  Arquivo: {CONFIG_PATH}", "grey")
    console.print(f"  Logs:    {LOG_FILE}", "grey")


def interactive_menu(ctx: Context) -> int:
    console = ctx.console
    winget_version = ctx.winget.version() if ctx.winget.executable else ""
    console.banner(__version__, winget_version)
    if not ctx.winget.executable:
        console.error("winget não encontrado. Instale o 'App Installer' pela Microsoft Store.")
        return 3

    actions = {
        "1": _menu_search,
        "2": _menu_download,
        "3": lambda context: _menu_install(context),
        "4": _menu_update,
        "5": _menu_remove,
        "6": _menu_list,
        "7": _menu_clean,
        "8": _menu_doctor,
        "9": _menu_config,
    }

    while True:
        console.print()
        console.print("  1. Procurar pacotes          6. Listar instalados")
        console.print("  2. Baixar instalador         7. Limpar resíduos")
        console.print("  3. Instalar pacote           8. Diagnóstico")
        console.print("  4. Verificar/atualizar       9. Configuração")
        console.print("  5. Remover pacote            0. Sair")
        console.print()
        choice = console.ask("Escolha uma opção", default="0").strip()
        if choice == "0":
            console.info("Até logo.")
            return 0
        action = actions.get(choice)
        if action is None:
            console.warn("Opção inválida.")
            continue
        try:
            action(ctx)
        except UserCancelled as exc:
            console.warn(str(exc) or "Operação cancelada.")
        except WpmError as exc:
            console.error(str(exc))
            if not is_admin():
                console.hint("Se for permissão, execute 'wpm <comando> --elevate'.")
            console.hint(f"Log: {LOG_FILE}")
        except KeyboardInterrupt:
            console.print()
            console.warn("Operação interrompida; voltando ao menu.")


def _menu_install(ctx: Context) -> None:
    query = ctx.console.ask("Nome ou ID do pacote", required=True)
    package = _resolve_remote(ctx, query)
    service_install(ctx, package)


# ---------------------------------------------------------------------------
# Parser e entrada principal
# ---------------------------------------------------------------------------


def _add_common_options(target: argparse.ArgumentParser, *, suppress: bool) -> None:
    """Adiciona flags globais; nos subcomandos usamos SUPPRESS para não sobrescrever."""
    default = argparse.SUPPRESS if suppress else False
    target.add_argument("--no-color", action="store_true", default=default, help="desativa cores na saída")
    target.add_argument("-v", "--verbose", action="store_true", default=default, help="mostra detalhes adicionais")
    target.add_argument(
        "-y",
        "--yes",
        dest="assume_yes",
        action="store_true",
        default=default,
        help="confirma automaticamente",
    )
    target.add_argument(
        "--timeout",
        type=int,
        metavar="SEG",
        default=argparse.SUPPRESS if suppress else None,
        help="tempo limite das operações (segundos)",
    )


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    _add_common_options(common, suppress=False)
    sub_common = argparse.ArgumentParser(add_help=False)
    _add_common_options(sub_common, suppress=True)

    parser = argparse.ArgumentParser(
        prog="wpm",
        description="Gerenciador de pacotes para Windows baseado no winget.",
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[common],
    )
    subparsers = parser.add_subparsers(dest="command", title="comandos")

    search = subparsers.add_parser(
        "search", parents=[sub_common], help="procura pacotes", aliases=["find"]
    )
    search.add_argument("query", nargs="+", help="termo de busca")
    search.add_argument("--limit", type=_limit_value, default=None, help="máximo de resultados (1-200)")
    search.add_argument("--source", help="fonte específica (ex.: winget, msstore)")
    search.add_argument("--install", action="store_true", help="pergunta se deseja instalar")

    install = subparsers.add_parser(
        "install", parents=[sub_common], help="instala um pacote", aliases=["add"]
    )
    install.add_argument("package", nargs="+", help="nome, moniker ou ID do pacote")
    install.add_argument("--id", action="store_true", help="trata o termo como ID exato")
    install.add_argument("--version", help="versão específica")
    install.add_argument("--scope", choices=("user", "machine"), help="escopo da instalação")
    install.add_argument("--location", help="pasta de instalação (se suportado)")
    install.add_argument("-i", "--interactive", action="store_true", help="instalação guiada")
    install.add_argument("--elevate", action="store_true", help="relança como administrador via UAC")
    install.add_argument("--dry-run", action="store_true", help="apenas mostra o que seria feito")
    install.add_argument("--source", help="fonte específica")

    download = subparsers.add_parser(
        "download", parents=[sub_common], help="baixa o instalador", aliases=["fetch"]
    )
    download.add_argument("package", nargs="+", help="nome ou ID do pacote")
    download.add_argument("--id", action="store_true", help="trata o termo como ID exato")
    download.add_argument("--version", help="versão específica")
    download.add_argument("-d", "--dir", help="pasta de destino")
    download.add_argument("--source", help="fonte específica")
    download.add_argument("--dry-run", action="store_true", help="apenas mostra o que seria feito")

    list_cmd = subparsers.add_parser(
        "list", parents=[sub_common], help="lista pacotes instalados", aliases=["ls"]
    )
    list_cmd.add_argument("filter", nargs="*", help="filtro por nome ou ID")
    list_cmd.add_argument("-u", "--upgradable", action="store_true", help="somente atualizações")
    list_cmd.add_argument("--source", help="fonte específica")

    update = subparsers.add_parser(
        "update", parents=[sub_common], help="atualiza pacotes", aliases=["upgrade"]
    )
    update.add_argument("package", nargs="*", help="pacote(s) a atualizar")
    update.add_argument("-a", "--all", action="store_true", help="atualiza todos os pacotes")
    update.add_argument("--include-unknown", action="store_true", help="inclui versões desconhecidas")
    update.add_argument("-i", "--interactive", action="store_true", help="atualização guiada")
    update.add_argument("--elevate", action="store_true", help="relança como administrador via UAC")
    update.add_argument("--dry-run", action="store_true", help="apenas mostra o que seria feito")
    update.add_argument("--source", help="fonte específica")

    remove = subparsers.add_parser(
        "remove", parents=[sub_common], help="desinstala e limpa resíduos", aliases=["uninstall", "rm"]
    )
    remove.add_argument("package", nargs="*", help="nome ou ID do pacote")
    remove.add_argument("-i", "--interactive", action="store_true", help="desinstalação guiada")
    remove.add_argument("--purge", action="store_true", help="procura e remove resíduos")
    remove.add_argument("--permanent", action="store_true", help="exclui resíduos permanentemente")
    remove.add_argument("--no-system", action="store_true", help="não procura em Program Files/ProgramData")
    remove.add_argument("--elevate", action="store_true", help="relança como administrador via UAC")
    remove.add_argument("--dry-run", action="store_true", help="apenas mostra o que seria feito")

    clean = subparsers.add_parser(
        "clean", parents=[sub_common], help="remove resíduos de aplicativos", aliases=["purge"]
    )
    clean.add_argument("name", nargs="*", help="nome ou ID do aplicativo")
    clean.add_argument("--permanent", action="store_true", help="exclui permanentemente (sem Lixeira)")
    clean.add_argument("--no-system", action="store_true", help="não procura em Program Files/ProgramData")
    clean.add_argument("--elevate", action="store_true", help="relança como administrador via UAC")
    clean.add_argument("--dry-run", action="store_true", help="apenas lista os candidatos")

    subparsers.add_parser("doctor", parents=[sub_common], help="diagnostica o ambiente", aliases=["check"])

    config = subparsers.add_parser("config", parents=[sub_common], help="mostra ou altera configurações")
    config.add_argument("--show", action="store_true", help="mostra a configuração")
    config.add_argument("--get", metavar="CHAVE", help="imprime uma chave")
    config.add_argument("--set", metavar="CHAVE=VALOR", action="append", help="altera uma chave")
    config.add_argument("--reset", action="store_true", help="restaura os padrões")

    subparsers.add_parser("version", parents=[sub_common], help="mostra as versões")

    subparsers.add_parser("menu", parents=[sub_common], help="abre o menu interativo")

    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    raw_args = list(argv) if argv is not None else list(sys.argv[1:])
    parser = build_parser()
    try:
        args = parser.parse_args(raw_args)
    except SystemExit as exc:
        return int(exc.code or 0)

    config = Config.load()
    if getattr(args, "timeout", None):
        if args.timeout < 30:
            config.timeout = 30
        else:
            config.timeout = args.timeout
        args.timeout = config.timeout
    if getattr(args, "source", None):
        config.source = args.source

    console = _build_console(args, config)
    configure_logging(verbose=getattr(args, "verbose", False), level=config.log_level)
    if config.load_warning:
        console.warn(config.load_warning)

    ctx = Context(
        config=config,
        console=console,
        winget=Winget(config, console),
        assume_yes=getattr(args, "assume_yes", False),
        verbose=getattr(args, "verbose", False),
        raw_args=raw_args,
    )
    LOGGER.info("wpm %s iniciado: %s", __version__, raw_args)

    try:
        if not args.command:
            if console.is_tty:
                return interactive_menu(ctx)
            parser.print_help()
            return 0
        handler = HANDLERS.get(args.command)
        if handler is None:
            parser.print_help()
            return 2
        return handler(ctx, args) or 0
    except UserCancelled as exc:
        console.warn(str(exc) or "Operação cancelada.")
        return exc.exit_code
    except WingetNotFoundError as exc:
        console.error(str(exc))
        console.hint("Instale o App Installer: https://aka.ms/winget-cli", file=console.err)
        return exc.exit_code
    except WpmError as exc:
        console.error(str(exc))
        if exc.exit_code == 7:
            console.hint(
                "Se o erro for de permissão, tente novamente com --elevate.",
                file=console.err,
            )
        console.hint(f"Log: {LOG_FILE}", file=console.err)
        return exc.exit_code
    except KeyboardInterrupt:
        console.print()
        console.warn("Interrompido pelo usuário.")
        return 130
    except Exception as exc:  # pragma: no cover - proteção final
        LOGGER.exception("erro inesperado")
        console.error(f"Erro inesperado: {exc}")
        console.hint(f"Consulte o log: {LOG_FILE}", file=console.err)
        return 1
