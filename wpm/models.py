"""Modelos de dados compartilhados entre os módulos."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Package:
    """Representa um pacote retornado pelo winget."""

    name: str
    id: str
    version: str = ""
    available: str = ""
    match: str = ""
    source: str = ""

    @property
    def is_upgradable(self) -> bool:
        """Indica se o pacote possui uma versão mais nova disponível."""
        return bool(self.available and self.available != self.version)

    @property
    def display_name(self) -> str:
        """Nome de exibição com fallback para o identificador."""
        return self.name.strip() or self.id

    @property
    def is_winget_managed(self) -> bool:
        """Heurística: pacotes ARP/MSIX não possuem ID gerenciável pelo winget."""
        return "\\" not in self.id and bool(self.source)


@dataclass(frozen=True)
class CommandResult:
    """Resultado da execução de um processo externo."""

    args: tuple[str, ...]
    returncode: int
    output: str = ""
    duration: float = 0.0
    timed_out: bool = False

    @property
    def succeeded(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    @property
    def unsigned_code(self) -> int:
        """Converte o código HRESULT (negativo em Python) para 32 bits."""
        return self.returncode & 0xFFFFFFFF


@dataclass
class Residual:
    """Pasta potencialmente deixada por um aplicativo desinstalado."""

    path: Path
    size: int | None = None
    reason: str = ""
    exact: bool = False
    requires_admin: bool = False
    tags: list[str] = field(default_factory=list)
