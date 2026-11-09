"""Local, free source analysis: citation extraction, URL normalization, dedup.

Rule (anti-false-consensus): several responses citing the same underlying
source are ONE line of evidence, not several confirmations.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit

URL_RE = re.compile(r"https?://[^\s<>\[\]()\"'`]+")
_TRACKING = re.compile(r"^(utm_|ref$|ref_src$|fbclid$|gclid$|mc_)")


def normalize_url(url: str) -> str | None:
    url = url.rstrip(".,;:!?*_")
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if not host or "." not in host:
        return None
    host = host.removeprefix("www.").removeprefix("m.")
    path = parts.path.rstrip("/")
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if not _TRACKING.match(k)])
    return host + path + (f"?{query}" if query else "")


def extract_sources(text: str | None, citations: Iterable[Any] = ()) -> list[str]:
    found: list[str] = []
    candidates = list(URL_RE.findall(text or ""))
    for c in citations or []:
        if isinstance(c, str):
            candidates.append(c)
        elif isinstance(c, dict) and c.get("url"):
            candidates.append(str(c["url"]))
    for u in candidates:
        n = normalize_url(u)
        if n and n not in found:
            found.append(n)
    return found


@dataclass
class SourceReport:
    per_response: dict[str, list[str]] = field(default_factory=dict)  # key (provider or label) -> sources
    shared: list[tuple[str, list[str]]] = field(default_factory=list)  # (source, keys citing it) when >= 2
    uncited: list[str] = field(default_factory=list)

    def evidence_lines(self, keys: Iterable[str]) -> tuple[int, int, list[str]]:
        """For a group of responses: (cited responses, independent source groups, shared sources).

        Responses that share any source are merged into one evidence line (union-find).
        """
        keys = [k for k in keys if k in self.per_response]
        cited = [k for k in keys if self.per_response[k]]
        parent = {k: k for k in cited}

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        shared_here: list[str] = []
        for src, who in self.shared:
            group = [k for k in who if k in parent]
            if len(group) >= 2:
                shared_here.append(src)
                for k in group[1:]:
                    parent[find(k)] = find(group[0])
        return len(cited), len({find(k) for k in cited}), shared_here


def analyze_sources(responses: dict[str, tuple[str | None, Iterable[Any]]]) -> SourceReport:
    """responses: key -> (text, provider citations)."""
    rep = SourceReport()
    for key, (text, cites) in responses.items():
        rep.per_response[key] = extract_sources(text, cites)
    index: dict[str, list[str]] = {}
    for key, srcs in rep.per_response.items():
        for s in srcs:
            index.setdefault(s, []).append(key)
    rep.shared = sorted(((s, ks) for s, ks in index.items() if len(ks) >= 2), key=lambda x: -len(x[1]))
    rep.uncited = [k for k, srcs in rep.per_response.items() if not srcs]
    return rep
