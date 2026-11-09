"""API key resolution and redaction.

Keys come from environment variables or from an optional *argv list*
command (e.g. a password manager CLI). Keys are never written to the database,
never logged, and scrubbed from any error text that reaches the UI or storage.
"""

from __future__ import annotations

import os
import re
import subprocess
from typing import Mapping, Sequence

_KNOWN: set[str] = set()

# Common key shapes, scrubbed even if we never saw the value ourselves.
_KEY_PATTERNS = [
    re.compile(r"sk-(?:ant-|proj-)?[A-Za-z0-9_\-]{16,}"),
    re.compile(r"xai-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"pplx-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),
]


def register_secret(value: str) -> None:
    if value and len(value) >= 8:
        _KNOWN.add(value)


def redact(text: str) -> str:
    if not text:
        return text
    for s in _KNOWN:
        if s in text:
            text = text.replace(s, "***")
    for pat in _KEY_PATTERNS:
        text = pat.sub("***", text)
    return text


class SecretResolver:
    def __init__(self, env: Mapping[str, str] | None = None) -> None:
        self._env = env if env is not None else os.environ
        self._cache: dict[str, str | None] = {}

    def resolve(
        self,
        provider: str,
        env_names: Sequence[str],
        command: Sequence[str] | None = None,
    ) -> str | None:
        if provider in self._cache:
            return self._cache[provider]
        value: str | None = None
        for name in env_names:
            v = self._env.get(name, "").strip()
            if v:
                value = v
                break
        if value is None and command:
            value = self._run_command(provider, command)
        if value:
            register_secret(value)
        self._cache[provider] = value
        return value

    @staticmethod
    def _run_command(provider: str, command: Sequence[str]) -> str | None:
        if isinstance(command, str) or not all(isinstance(c, str) for c in command):
            raise ValueError(
                f"{provider}: api_key_command must be a list of strings (argv), not a shell string"
            )
        try:
            out = subprocess.run(  # noqa: S603 - argv list, no shell
                list(command), capture_output=True, text=True, timeout=20, check=False
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if out.returncode != 0:
            return None
        return out.stdout.strip() or None
