"""One-click helper lane: you run the prompt in the provider's own app; Cockpit does the rest.

Used for Perplexity, which offers no sanctioned programmatic access to a Pro
subscription and whose terms forbid automated/bot use of its website. A human
using the product stays within those terms and uses your subscription.

Flow per round:
  1. Cockpit copies the lane's exact prompt to your clipboard and opens your own
     browser (new conversation: perplexity.ai/search?q=...; follow-up: paste into
     the Perplexity thread you are already using for this chat).
  2. The other lanes stream as usual; this lane shows "waiting for you".
  3. You copy Perplexity's answer; Cockpit reads your clipboard (or a file /
     stdin via `cockpit lane paste perplexity`) and commits it like any answer.

Cockpit never touches the website, cookies or your credentials.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import webbrowser
from dataclasses import dataclass
from typing import Any, AsyncIterator
from urllib.parse import quote

from .base import AdapterEvent, ChatRequest, ErrorKind, HealthReport, ProviderAdapter, ProviderError

MAX_URL_PROMPT = 1500  # longer prompts go via clipboard only

SITES = {
    "perplexity": ("https://www.perplexity.ai/", "https://www.perplexity.ai/search?q={q}"),
}


class ManualIO:
    """Clipboard + browser access on the user's own computer. Swappable for tests."""

    def _first(self, *cmds: list[str]) -> list[str] | None:
        for c in cmds:
            if shutil.which(c[0]):
                return c
        return None

    def copy(self, text: str) -> bool:
        cmd = self._first(["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"], ["clip.exe"])
        if not cmd:
            return False
        try:
            subprocess.run(cmd, input=text.encode(), check=True, timeout=10)  # argv list, no shell
            return True
        except (OSError, subprocess.SubprocessError):
            return False

    def paste(self) -> str | None:
        cmd = self._first(["pbpaste"], ["wl-paste", "--no-newline"], ["xclip", "-selection", "clipboard", "-o"])
        if not cmd:
            return None
        try:
            return subprocess.run(cmd, capture_output=True, check=True, timeout=10).stdout.decode("utf-8", "replace")
        except (OSError, subprocess.SubprocessError):
            return None

    def open_url(self, url: str) -> bool:
        if sys.platform != "darwin" and not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
            return False  # headless machine: nothing to open
        try:
            return bool(webbrowser.open(url, new=2))
        except webbrowser.Error:
            return False

    def available(self) -> dict[str, bool]:
        return {"clipboard": bool(self._first(["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"])),
                "browser": sys.platform == "darwin" or bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))}


@dataclass
class Handoff:
    mode: str  # new_thread | continue_thread | replay
    text: str  # exactly what the user should send
    url: str
    copied: bool = False
    opened: bool = False


class ManualAdapter(ProviderAdapter):
    transport = "manual"
    capabilities = frozenset({"subscription_auth", "web_search", "citations"})
    streaming_granularity = "manual"

    def __init__(self, config: Any, secrets: Any, io: ManualIO | None = None) -> None:
        super().__init__(config, secrets)
        self.io = io or ManualIO()

    def handoff(self, text: str, mode: str) -> Handoff:
        home, search = SITES.get(self.config.id, (self.config.base_url or "", ""))
        url = search.format(q=quote(text)) if (mode == "new_thread" and search and len(text) <= MAX_URL_PROMPT) else home
        h = Handoff(mode=mode, text=text, url=url)
        h.copied = self.io.copy(text)
        h.opened = self.io.open_url(url) if url else False
        return h

    async def stream(self, request: ChatRequest) -> AsyncIterator[AdapterEvent]:  # pragma: no cover - engine handles manual lanes
        raise ProviderError(ErrorKind.INTERNAL, f"{self.provider_id}: manual lanes are handled by the engine")
        yield  # type: ignore[unreachable]

    async def health_check(self, live: bool = False) -> HealthReport:
        report = self.new_report()
        avail = self.io.available()
        report.subscription_path = True
        report.notes.append("one-click helper: Cockpit opens the site in YOUR browser with the question; you copy the "
                            "answer back. No automation of the website, no API cost.")
        if not avail["clipboard"]:
            report.notes.append("no clipboard tool found here - answers can be pasted with "
                                f"`cockpit lane paste {self.provider_id} --file answer.md` (or via stdin)")
        if not avail["browser"]:
            report.notes.append("no desktop browser on this machine - the prompt is saved to a file for you to copy")
        return report

    async def live_ping(self, report: HealthReport) -> None:
        report.notes.append("live check skipped: this lane needs you in the loop")
