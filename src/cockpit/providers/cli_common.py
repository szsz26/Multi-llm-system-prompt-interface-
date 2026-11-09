"""Shared plumbing for subscription CLI transports.

* argv lists only (asyncio.create_subprocess_exec) - never a shell string.
* Prompts go to stdin unless a CLI can only take them as an argv element.
* Env vars that could make a CLI bill an API key instead of the subscription
  are removed from the child environment (``strip_env``).
* Each provider runs in its own empty working directory, never the user's repo.
* Cancelling the consuming task kills the process (that is how `stop` works).
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Sequence

from ..domain import ChatMessage
from ..secrets import redact
from .base import ErrorKind, ProviderError


def clean_env(strip: Sequence[str]) -> dict[str, str]:
    env = dict(os.environ)
    for name in strip:
        env.pop(name, None)
    return env


def which(binary: str, override: str | None = None) -> str | None:
    if override:
        p = Path(override).expanduser()
        return str(p) if p.exists() else None
    return shutil.which(binary)


def render_replay(messages: list[ChatMessage]) -> str:
    """Flatten canonical history into one prompt for a fresh CLI session."""
    if len(messages) == 1:
        return messages[0].content
    turns = "\n".join(f'<turn role="{m.role}">\n{m.content}\n</turn>' for m in messages[:-1])
    return (
        "Here is our conversation so far (restored from a saved transcript). "
        "Continue it naturally by answering my latest message.\n\n"
        f"<conversation>\n{turns}\n</conversation>\n\n{messages[-1].content}"
    )


def classify_cli_error(provider: str, text: str, label: str = "CLI") -> ProviderError:
    low = text.lower()
    msg = f"{provider} ({label}): {text.strip()[:400]}"
    if "no conversation found" in low or ("session" in low and "not found" in low):
        return ProviderError(ErrorKind.NATIVE_SESSION_LOST, msg)
    if any(w in low for w in ("usage limit", "rate limit", "rate_limit", "quota", "too many requests", "429")):
        return ProviderError(ErrorKind.RATE_LIMIT, msg)
    if any(w in low for w in ("not logged in", "login", "log in", "unauthorized", "401", "authenticat", "oauth", "api key", "expired")):
        return ProviderError(ErrorKind.AUTH, msg)
    if "model" in low and any(w in low for w in ("not found", "invalid", "not available", "not supported", "unknown")):
        return ProviderError(ErrorKind.MODEL_UNAVAILABLE, msg)
    if any(w in low for w in ("overloaded", "529", "503", "502", "500", "internal server")):
        return ProviderError(ErrorKind.SERVER, msg, retryable=True)
    if any(w in low for w in ("network", "connection", "timed out", "dns")):
        return ProviderError(ErrorKind.NETWORK, msg, retryable=True)
    return ProviderError(ErrorKind.INTERNAL, msg)


@dataclass
class CLIResult:
    returncode: int | None = None
    stderr: str = ""
    lines: list[str] = field(default_factory=list)


async def run_cli(
    provider: str,
    argv: list[str],
    *,
    stdin_text: str | None,
    cwd: Path,
    env: dict[str, str],
    idle_timeout: float,
    result: CLIResult,
    read_chunks: bool = False,
) -> AsyncIterator[str]:
    """Run a CLI and yield stdout lines (or raw chunks). Fills `result` when done."""
    cwd.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE if stdin_text is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd),
            env=env,
            limit=16 * 1024 * 1024,
        )
    except OSError as e:
        raise ProviderError(ErrorKind.NOT_INSTALLED, f"{provider}: cannot start {argv[0]}: {e}") from e

    stderr_buf = bytearray()

    async def drain_stderr() -> None:
        assert proc.stderr is not None
        while chunk := await proc.stderr.read(4096):
            if len(stderr_buf) < 20000:
                stderr_buf.extend(chunk)

    err_task = asyncio.create_task(drain_stderr())
    decoder = codecs.getincrementaldecoder("utf-8")("replace")  # never split a multi-byte char
    try:
        assert proc.stdout is not None
        if stdin_text is not None:
            assert proc.stdin is not None
            proc.stdin.write(stdin_text.encode())
            await proc.stdin.drain()
            proc.stdin.close()
        while True:
            try:
                if read_chunks:
                    data = await asyncio.wait_for(proc.stdout.read(4096), timeout=idle_timeout)
                else:
                    data = await asyncio.wait_for(proc.stdout.readline(), timeout=idle_timeout)
            except asyncio.TimeoutError as e:
                raise ProviderError(ErrorKind.TIMEOUT, f"{provider}: CLI produced no output for {idle_timeout:.0f}s") from e
            if not data:
                tail = decoder.decode(b"", final=True)
                if tail:
                    yield tail
                break
            text = decoder.decode(data)
            if not text:
                continue
            if not read_chunks:
                result.lines.append(text)
            yield text
        await proc.wait()
        await err_task
        result.returncode = proc.returncode
        result.stderr = redact(stderr_buf.decode("utf-8", "replace"))
    finally:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
        err_task.cancel()


async def cli_version(binary: str, args: Sequence[str] = ("--version",)) -> str | None:
    try:
        proc = await asyncio.create_subprocess_exec(
            binary, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        return out.decode("utf-8", "replace").strip()[:200] or None
    except (OSError, asyncio.TimeoutError):
        return None
