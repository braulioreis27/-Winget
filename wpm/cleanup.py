"""Busca e remoção conservadora de resíduos deixados por aplicativos."""

from __future__ import annotations

import os
import re
import shutil
import unicodedata
from pathlib import Path

from .errors import WpmError
from .models import Residual
from .platform_win import send_to_recycle_bin

_GENERIC_TOKENS = {
    "app",
    "apps",
    "application",
    "aplicativo",
    "cache",
    "client",
    "comum",
    "common",
    "data",
    "dados",
    "desktop",
    "driver",
    "drivers",
    "instalador",
    "installer",
    "launcher",
    "local",
    "microsoft",
    "program",
    "programa",
    "programas",
    "programs",
    "roaming",
    "runtime",
    "service",
    "services",
    "setup",
    "system",
    "sistema",
    "studio",
    "temp",
    "temporario",
    "tool",
    "tools",
    "update",
    "updater",
    "windows",
    "x64",
    "x86",
    "arm64",
}

_PROTECTED_NAMES = {
    "windows",
    "microsoft",
    "programdata",
    "program files",
    "program files x86",
    "users",
    "default",
    "public",
    "common files",
    "packages",
    "temp",
    "cache",
}


def normalize(text: str) -> str:
    """Normaliza texto para comparação sem acentos e sem pontuação."""
    decomposed = unicodedata.normalize("NFKD", text)
    without_accents = "".join(char for char in decomposed if not unicodedata.combining(char))
    cleaned = re.sub(r"[^a-z0-9]+", " ", without_accents.casefold())
    return " ".join(cleaned.split())


def _build_tokens(name: str, package_id: str) -> tuple[set[str], str | None]:
    """Gera tokens específicos do aplicativo e o fornecedor (publisher)."""
    normalized_name = normalize(name)
    parts = [part for part in package_id.split(".") if part.strip()]
    publisher = normalize(parts[0]) if len(parts) > 1 else None
    last_part = normalize(parts[-1]) if parts else ""

    raw_tokens = {normalized_name, normalized_name.replace(" ", ""), last_part, last_part.replace(" ", "")}
    tokens = {
        token
        for token in raw_tokens
        if len(token) >= 3 and token not in _GENERIC_TOKENS
    }
    return tokens, publisher or None


def _match_name(folder_name: str, tokens: set[str], *, exact_only: bool) -> tuple[bool, bool]:
    """Retorna (corresponde, é_exato) para o nome de uma pasta."""
    normalized = normalize(folder_name)
    compact = normalized.replace(" ", "")
    if normalized in tokens or compact in tokens:
        return True, True
    if exact_only or not tokens:
        return False, False
    for token in tokens:
        if len(token) < 4:
            continue
        if " " in token:
            if token in normalized:
                return True, False
        elif re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", normalized):
            return True, False
    return False, False


def directory_size(path: Path, max_entries: int = 25_000) -> int | None:
    """Calcula o tamanho de uma pasta com limite de entradas.

    Retorna None quando o limite é atingido ou o acesso é negado, evitando
    travar a interface em pastas muito grandes.
    """
    total = 0
    entries = 0
    stack = [path]
    try:
        while stack:
            current = stack.pop()
            with os.scandir(current) as iterator:
                for entry in iterator:
                    entries += 1
                    if entries > max_entries:
                        return None
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            child = Path(entry.path)
                            if _is_reparse_point(child):
                                continue
                            stack.append(child)
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
    except OSError:
        return None
    return total


def _is_reparse_point(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if callable(is_junction) and is_junction():
            return True
        stat = path.stat(follow_symlinks=False)
        return bool(getattr(stat, "st_file_attributes", 0) & 0x400)
    except OSError:
        return True


def is_protected(path: Path) -> bool:
    """Impede a remoção de raízes de sistema, do perfil e do diretório atual."""
    try:
        resolved = path.resolve()
    except OSError:
        return True

    if normalize(resolved.name) in _PROTECTED_NAMES:
        return True
    if resolved.parent == resolved:
        return True

    cwd = Path.cwd().resolve()
    if resolved == cwd:
        return True
    try:
        if cwd.is_relative_to(resolved):
            return True
    except (AttributeError, ValueError):
        pass

    protected: set[Path] = set()
    for variable, fallback in (
        ("LOCALAPPDATA", None),
        ("APPDATA", None),
        ("PROGRAMDATA", None),
        ("PROGRAMFILES", None),
        ("PROGRAMFILES(X86)", None),
        ("WINDIR", None),
    ):
        raw = os.environ.get(variable, fallback or "")
        if raw:
            try:
                protected.add(Path(raw).resolve())
            except OSError:
                continue
    try:
        protected.add(Path.home().resolve())
    except OSError:
        pass

    if resolved in protected:
        return True

    windows_dir = os.environ.get("WINDIR", "")
    if windows_dir:
        try:
            if resolved.is_relative_to(Path(windows_dir).resolve()):
                return True
        except (AttributeError, OSError, ValueError):
            pass
    return False


def _scan_directory(
    directory: Path,
    *,
    tokens: set[str],
    exact_only: bool,
    label: str,
    requires_admin: bool,
    results: dict[Path, Residual],
) -> None:
    try:
        if not directory.is_dir() or _is_reparse_point(directory):
            return
        children = list(directory.iterdir())
    except OSError:
        return

    for child in children:
        try:
            if not child.is_dir() or _is_reparse_point(child):
                continue
        except OSError:
            continue
        matched, exact = _match_name(child.name, tokens, exact_only=exact_only)
        if not matched:
            continue
        if is_protected(child):
            continue
        try:
            resolved = child.resolve()
        except OSError:
            continue
        if resolved in results:
            continue
        reason = "nome exato do aplicativo" if exact else "nome semelhante ao aplicativo"
        results[resolved] = Residual(
            path=resolved,
            size=directory_size(resolved),
            reason=reason,
            exact=exact,
            requires_admin=requires_admin,
            tags=[label],
        )


def scan_residuals(name: str, package_id: str = "", *, include_system: bool = True) -> list[Residual]:
    """Procura pastas que provavelmente pertenciam ao aplicativo.

    A busca é limitada a raízes conhecidas de dados/programas e exige que o
    nome da pasta corresponda ao nome do aplicativo (nunca apenas ao fornecedor).
    """
    tokens, publisher = _build_tokens(name, package_id)
    if not tokens:
        return []

    local = os.environ.get("LOCALAPPDATA", "")
    roaming = os.environ.get("APPDATA", "")
    program_data = os.environ.get("PROGRAMDATA", "")
    program_files = os.environ.get("PROGRAMFILES", "")
    program_files_x86 = os.environ.get("PROGRAMFILES(X86)", "")

    bases: list[tuple[Path, str, bool, bool]] = []
    if local:
        bases.append((Path(local), "dados locais", False, False))
        bases.append((Path(local) / "Programs", "programas do usuário", False, False))
    if roaming:
        bases.append((Path(roaming), "dados roaming", False, False))
    if include_system and program_data:
        bases.append((Path(program_data), "dados compartilhados", False, True))
    if include_system and program_files:
        bases.append((Path(program_files), "Arquivos de Programas", True, True))
    if include_system and program_files_x86:
        bases.append((Path(program_files_x86), "Arquivos de Programas (x86)", True, True))

    results: dict[Path, Residual] = {}

    for root, label, exact_only, requires_admin in bases:
        _scan_directory(
            root,
            tokens=tokens,
            exact_only=exact_only,
            label=label,
            requires_admin=requires_admin,
            results=results,
        )
        if publisher:
            _scan_directory(
                root / publisher,
                tokens=tokens,
                exact_only=exact_only,
                label=f"{label} (fornecedor)",
                requires_admin=requires_admin,
                results=results,
            )

    residuals = list(results.values())
    residuals.sort(key=lambda item: (not item.exact, -(item.size or 0)))
    return residuals


def delete_residual(residual: Residual, *, permanent: bool = False) -> None:
    """Remove um resíduo para a Lixeira (ou permanentemente, se solicitado)."""
    path = Path(residual.path)
    if not path.exists():
        raise WpmError(f"O caminho não existe mais: {path}")
    if is_protected(path):
        raise WpmError(f"Remoção bloqueada por segurança: {path}")
    if _is_reparse_point(path):
        raise WpmError(f"Remoção bloqueada: link ou junção não é seguido por segurança: {path}")
    if permanent:
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
        except OSError as exc:
            raise WpmError(f"Não foi possível excluir '{path}': {exc}") from exc
        return
    try:
        send_to_recycle_bin([path])
    except OSError as exc:
        raise WpmError(
            f"Não foi possível mover '{path}' para a Lixeira: {exc}. "
            "Execute como administrador ou use --permanent."
        ) from exc
