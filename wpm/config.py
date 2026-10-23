"""Configuração persistente e logging do WPM."""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field, fields
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from .errors import ConfigurationError

APP_NAME = "wpm"
LOGGER_NAME = "wpm"
VALID_COLORS = ("auto", "always", "never")
VALID_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def _env_path(variable: str, fallback: Path) -> Path:
    value = os.environ.get(variable)
    return Path(value) if value else fallback


def app_data_dir() -> Path:
    """Pasta de configuração por usuário (%APPDATA%\\wpm)."""
    return _env_path("APPDATA", Path.home() / "AppData" / "Roaming") / APP_NAME


def local_data_dir() -> Path:
    """Pasta de dados locais por usuário (%LOCALAPPDATA%\\wpm)."""
    return _env_path("LOCALAPPDATA", Path.home() / "AppData" / "Local") / APP_NAME


CONFIG_PATH = app_data_dir() / "config.json"
LOG_DIR = local_data_dir() / "logs"
LOG_FILE = LOG_DIR / "wpm.log"
DEFAULT_DOWNLOAD_DIR = Path.home() / "Downloads" / "wpm"


@dataclass
class Config:
    """Preferências do usuário, serializadas em JSON."""

    silent_install: bool = True
    checksum_after_download: bool = True
    download_dir: str = str(DEFAULT_DOWNLOAD_DIR)
    source: str = "auto"
    update_sources: bool = True
    color: str = "auto"
    timeout: int = 1800
    search_limit: int = 25
    log_level: str = "INFO"
    load_warning: str = field(default="", compare=False, repr=False)

    @property
    def download_path(self) -> Path:
        """Caminho de download expandido e pronto para uso."""
        expanded = os.path.expandvars(os.path.expanduser(self.download_dir))
        return Path(expanded)

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        """Lê a configuração do disco, usando padrões quando necessário."""
        target = path or CONFIG_PATH
        config = cls()
        if not target.exists():
            return config

        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("o conteúdo raiz não é um objeto JSON")
            config._apply(raw)
        except (OSError, ValueError, TypeError, json.JSONDecodeError, ConfigurationError) as exc:
            backup = target.with_suffix(f".invalid-{int(time.time())}.json")
            try:
                target.replace(backup)
                backup_note = f" Backup criado em {backup}."
            except OSError:
                backup_note = ""
            config = cls()
            config.load_warning = f"Configuração inválida ({exc}).{backup_note}"
        return config

    def _apply(self, values: dict[str, Any]) -> None:
        """Aplica e valida valores vindos do JSON ou de `wpm config --set`."""
        valid = {item.name for item in fields(self) if item.name != "load_warning"}
        for key, value in values.items():
            if key not in valid:
                continue
            if key in ("silent_install", "checksum_after_download", "update_sources"):
                if not isinstance(value, bool):
                    raise ConfigurationError(f"'{key}' deve ser true ou false.")
            elif key in ("timeout", "search_limit"):
                if not isinstance(value, int) or isinstance(value, bool):
                    raise ConfigurationError(f"'{key}' deve ser um número inteiro.")
                if key == "timeout" and not (30 <= value <= 86_400):
                    raise ConfigurationError("'timeout' deve estar entre 30 e 86400 segundos.")
                if key == "search_limit" and not (1 <= value <= 200):
                    raise ConfigurationError("'search_limit' deve estar entre 1 e 200.")
            elif key == "color":
                if str(value).lower() not in VALID_COLORS:
                    raise ConfigurationError("'color' deve ser auto, always ou never.")
                value = str(value).lower()
            elif key == "log_level":
                if str(value).upper() not in VALID_LEVELS:
                    raise ConfigurationError(
                        "'log_level' deve ser DEBUG, INFO, WARNING, ERROR ou CRITICAL."
                    )
                value = str(value).upper()
            elif key == "source":
                if not isinstance(value, str) or not value.strip():
                    raise ConfigurationError("'source' não pode ser vazia (use 'auto').")
                value = value.strip()
            elif key == "download_dir":
                if not isinstance(value, str) or not value.strip():
                    raise ConfigurationError("'download_dir' não pode ser vazio.")
                value = value.strip()
            setattr(self, key, value)

    def set(self, key: str, value: Any) -> None:
        """Valida e define uma única chave."""
        self._apply({key: value})

    def to_public_dict(self) -> dict[str, Any]:
        """Representação serializável, sem campos internos de estado."""
        data = asdict(self)
        data.pop("load_warning", None)
        return data

    def save(self, path: Path | None = None) -> Path:
        """Grava a configuração de forma atômica."""
        target = path or CONFIG_PATH
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(self.to_public_dict(), indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, target)
        except OSError as exc:
            raise ConfigurationError(f"Não foi possível salvar a configuração: {exc}") from exc
        return target


def configure_logging(verbose: bool = False, level: str = "INFO") -> logging.Logger:
    """Configura log em arquivo rotativo e, opcionalmente, no console."""
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    _ = level

    if getattr(logger, "_wpm_configured", False):
        for handler in logger.handlers:
            if isinstance(handler, logging.StreamHandler) and not isinstance(
                handler, RotatingFileHandler
            ):
                handler.setLevel(logging.DEBUG if verbose else logging.WARNING)
        return logger

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            LOG_FILE, maxBytes=1_048_576, backupCount=3, encoding="utf-8"
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except OSError:
        pass

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG if verbose else logging.WARNING)
    console_handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    logger.addHandler(console_handler)
    logger.setLevel(logging.DEBUG)
    logger._wpm_configured = True  # type: ignore[attr-defined]
    return logger
