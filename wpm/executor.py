"""Execução de processos externos com streaming e captura de saída."""

from __future__ import annotations

import codecs
import os
import queue
import re
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from .errors import WingetNotFoundError
from .models import CommandResult

_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize(text: str) -> str:
    """Remove códigos ANSI e caracteres de controle problemáticos."""
    text = _ANSI_RE.sub("", text)
    text = _CONTROL_RE.sub("", text)
    return text.rstrip()


def decode_output(data: bytes) -> str:
    """Decodifica a saída do winget (UTF-8) com fallbacks legados."""
    for encoding in ("utf-8", "cp1252", "cp850"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _terminate_tree(process: subprocess.Popen[bytes]) -> None:
    """Encerra o processo e seus filhos (instaladores) de forma agressiva."""
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        process.kill()
    except OSError:
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _popen(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> subprocess.Popen[bytes]:
    merged_env = None
    if env is not None:
        merged_env = os.environ.copy()
        merged_env.update(env)
    try:
        return subprocess.Popen(
            list(args),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            cwd=str(cwd) if cwd else None,
            env=merged_env,
        )
    except FileNotFoundError as exc:
        raise WingetNotFoundError(
            f"Executável não encontrado: {args[0]}. Verifique se o winget está instalado."
        ) from exc
    except OSError as exc:
        raise WingetNotFoundError(f"Não foi possível iniciar '{args[0]}': {exc}") from exc


def run_capture(
    args: Sequence[str],
    *,
    timeout: float | None = None,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> CommandResult:
    """Executa um comando e captura toda a saída (sem streaming)."""
    started = time.monotonic()
    process = _popen(args, cwd=cwd, env=env)
    timed_out = False
    try:
        try:
            data, _ = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _terminate_tree(process)
            data, _ = process.communicate()
    finally:
        if process.poll() is None:
            _terminate_tree(process)
    return CommandResult(
        args=tuple(str(arg) for arg in args),
        returncode=process.returncode if process.returncode is not None else -1,
        output=decode_output(data or b""),
        duration=time.monotonic() - started,
        timed_out=timed_out,
    )


def stream_command(
    args: Sequence[str],
    on_text: Callable[[str], None],
    *,
    timeout: float | None = None,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> CommandResult:
    """Executa um comando repassando cada linha/atualização de progresso.

    O winget usa `\\r` para atualizar barras de progresso. Nós normalizamos
    esse caractere para que o callback receba cada atualização separadamente.
    """
    started = time.monotonic()
    process = _popen(args, cwd=cwd, env=env)
    assert process.stdout is not None

    line_queue: queue.Queue[bytes | None] = queue.Queue()

    def _reader() -> None:
        try:
            while True:
                chunk = process.stdout.read(4096)
                if not chunk:
                    break
                line_queue.put(chunk)
        finally:
            line_queue.put(None)

    threading.Thread(target=_reader, name="wpm-stdout", daemon=True).start()

    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    collected: list[str] = []
    pending = ""
    timed_out = False

    def _emit(text: str) -> None:
        nonlocal pending
        pending += text
        normalized = pending.replace("\r\n", "\n").replace("\r", "\n")
        lines = normalized.split("\n")
        pending = lines.pop()
        for line in lines:
            cleaned = sanitize(line)
            on_text(cleaned)

    try:
        while True:
            try:
                item = line_queue.get(timeout=0.2)
            except queue.Empty:
                if timeout is not None and time.monotonic() - started > timeout:
                    timed_out = True
                    _terminate_tree(process)
                continue
            if item is None:
                break
            text = decoder.decode(item)
            if text:
                collected.append(text)
                _emit(text)
        tail = decoder.decode(b"", final=True)
        if tail:
            collected.append(tail)
            _emit(tail)
        if pending:
            on_text(sanitize(pending))
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _terminate_tree(process)
    except KeyboardInterrupt:
        _terminate_tree(process)
        raise
    finally:
        if process.poll() is None:
            _terminate_tree(process)
        try:
            process.stdout.close()
        except OSError:
            pass

    return CommandResult(
        args=tuple(str(arg) for arg in args),
        returncode=process.returncode if process.returncode is not None else -1,
        output="".join(collected),
        duration=time.monotonic() - started,
        timed_out=timed_out,
    )
