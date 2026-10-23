"""Integrações de baixo nível com o Windows.

Concentra descoberta do winget, verificação de privilégios, habilitação de
cores ANSI, elevação via UAC e envio de pastas para a Lixeira.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Sequence

from .errors import AdminRequiredError, UserCancelled, WpmError

try:  # pragma: no cover - ctypes.wintypes não existe fora do Windows
    from ctypes import wintypes
except (ImportError, ValueError):  # pragma: no cover
    wintypes = None  # type: ignore[assignment]


IS_WINDOWS = os.name == "nt"

_WINGET_CANDIDATES = (
    Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WindowsApps" / "winget.exe",
    Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "WindowsApps" / "winget.exe",
)


def is_admin() -> bool:
    """Retorna True quando o processo atual está elevado."""
    if not IS_WINDOWS or wintypes is None:
        return os.geteuid() == 0 if hasattr(os, "geteuid") else False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def enable_ansi() -> bool:
    """Habilita o processamento de sequências ANSI no console do Windows."""
    if not IS_WINDOWS:
        return True
    if os.environ.get("NO_COLOR") is not None or os.environ.get("TERM") == "dumb":
        return False
    try:
        kernel32 = ctypes.windll.kernel32
        enabled = False
        for handle_id in (-11, -12):  # STD_OUTPUT_HANDLE e STD_ERROR_HANDLE
            handle = kernel32.GetStdHandle(handle_id)
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                if kernel32.SetConsoleMode(handle, mode.value | 0x0004):
                    enabled = True
        return enabled
    except Exception:
        return False


def find_winget() -> Path | None:
    """Localiza o executável do winget em PATH ou nos caminhos conhecidos."""
    found = shutil.which("winget")
    if found:
        return Path(found)
    for candidate in _WINGET_CANDIDATES:
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def terminal_width(default: int = 100) -> int:
    """Largura atual do terminal, com fallback seguro."""
    try:
        return max(40, shutil.get_terminal_size((default, 30)).columns)
    except OSError:
        return default


def free_space(path: Path) -> int:
    """Espaço livre em bytes no volume que contém o caminho."""
    try:
        target = path if path.exists() else path.anchor or path
        return shutil.disk_usage(str(target)).free
    except OSError:
        return 0


if IS_WINDOWS and wintypes is not None:
    class _SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", ctypes.c_ulong),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hIcon", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    class _SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR),
            ("pTo", wintypes.LPCWSTR),
            ("fFlags", wintypes.WORD),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", wintypes.LPCWSTR),
        ]


def relaunch_elevated(argv: Sequence[str], cwd: str | None = None) -> int:
    """Relança o processo atual via UAC e aguarda o término.

    Uma nova janela de console é aberta pelo Windows. O código de saída do
    processo elevado é devolvido ao processo original.
    """
    if is_admin():
        return 0
    if not IS_WINDOWS or wintypes is None:
        raise AdminRequiredError("Elevação automática disponível apenas no Windows.")

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    shell_execute = shell32.ShellExecuteExW
    shell_execute.argtypes = [ctypes.POINTER(_SHELLEXECUTEINFOW)]
    shell_execute.restype = ctypes.c_bool

    params = subprocess.list2cmdline([str(arg) for arg in argv])
    info = _SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x00000040  # SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "runas"
    info.lpFile = sys.executable
    info.lpParameters = params
    info.lpDirectory = cwd or os.getcwd()
    info.nShow = 1  # SW_SHOWNORMAL

    ctypes.set_last_error(0)
    if not shell_execute(ctypes.byref(info)):
        error = ctypes.get_last_error()
        if error == 1223:  # ERROR_CANCELLED (usuário recusou o UAC)
            raise UserCancelled("Elevação cancelada pelo usuário.")
        raise AdminRequiredError(
            f"Não foi possível solicitar privilégios de administrador (erro {error})."
        )

    if not info.hProcess:
        raise AdminRequiredError("O processo elevado não pôde ser iniciado.")

    try:
        kernel32.WaitForSingleObject(info.hProcess, 0xFFFFFFFF)  # INFINITE
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(exit_code)):
            raise AdminRequiredError("Não foi possível obter o código de saída do processo elevado.")
        return int(exit_code.value)
    finally:
        kernel32.CloseHandle(info.hProcess)


def send_to_recycle_bin(paths: Sequence[Path]) -> None:
    """Move pastas/arquivos para a Lixeira usando a API nativa do Windows."""
    if not paths:
        return
    if not IS_WINDOWS or wintypes is None:
        raise WpmError("Envio para a Lixeira disponível apenas no Windows.")

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    operation = shell32.SHFileOperationW
    operation.argtypes = [ctypes.POINTER(_SHFILEOPSTRUCTW)]
    operation.restype = ctypes.c_int

    from_list = "\0".join(str(Path(p).resolve()) for p in paths) + "\0\0"
    struct = _SHFILEOPSTRUCTW()
    struct.hwnd = None
    struct.wFunc = 3  # FO_DELETE
    struct.pFrom = from_list
    struct.pTo = None
    struct.fFlags = 0x0040 | 0x0010 | 0x0004 | 0x0400 | 0x0200
    # FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI | FOF_NOCONFIRMMKDIR
    struct.fAnyOperationsAborted = False

    ctypes.set_last_error(0)
    result = operation(ctypes.byref(struct))
    if result != 0 or struct.fAnyOperationsAborted:
        raise OSError(result, "Falha ao mover item para a Lixeira.")
