"""Exceções específicas do WPM.

Cada exceção carrega um código de saída próprio para que a CLI possa
devolver um status consistente ao shell/scripts.
"""

from __future__ import annotations


class WpmError(Exception):
    """Erro base do WPM."""

    exit_code = 1


class UserCancelled(WpmError):
    """O usuário cancelou uma operação interativa."""

    exit_code = 2


class WingetNotFoundError(WpmError):
    """O winget não foi localizado no sistema."""

    exit_code = 3


class AdminRequiredError(WpmError):
    """A operação exige privilégios de administrador."""

    exit_code = 4


class NetworkError(WpmError):
    """Falha de conexão ou de acesso às fontes de pacotes."""

    exit_code = 5


class PackageNotFoundError(WpmError):
    """Nenhum pacote corresponde ao critério informado."""

    exit_code = 6


class OperationFailedError(WpmError):
    """O winget falhou ao executar uma operação."""

    exit_code = 7


class ConfigurationError(WpmError):
    """Configuração inválida ou corrompida."""

    exit_code = 8
