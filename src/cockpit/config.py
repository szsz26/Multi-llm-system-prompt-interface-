"""Configuration: built-in lane defaults + optional TOML overrides.

Subscription-first: every lane defaults to the provider's official
subscription-backed CLI where one exists. Paid API use is a separate,
explicitly approved transport (see billing.py) - never a silent fallback.

Model ids and CLI presets are configuration, not code: they change often.
`cockpit doctor` checks what it can and says plainly what it could not verify.
"""

from __future__ import annotations

import difflib
import os
import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any

from .domain import Billing, CockpitError

# API adapter families (used when a lane runs over a paid API).
KINDS = ("anthropic", "openai", "gemini", "xai", "perplexity", "openai_compat", "fake")
TRANSPORTS = ("claude-code", "codex", "grok", "command", "manual", "api", "fake")
TRANSPORT_ALIASES = {"cli": "claude-code"}
SUBSCRIPTION_TRANSPORTS = {"claude-code", "codex", "grok", "command", "manual"}


@dataclass
class ProviderConfig:
    id: str
    kind: str  # API adapter family for this provider
    display_name: str
    model: str  # model for the primary transport ("default" = let the CLI choose)
    transport: str = "api"
    api_model: str | None = None  # model when this lane runs over the API (defaults to `model`)
    effort: str | None = None  # default reasoning effort, where the transport supports it
    base_url: str | None = None
    api_key_env: list[str] = field(default_factory=list)
    api_key_command: list[str] | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    system_prompt: str | None = None  # visible lane instruction (none by default)
    timeout: float = 600.0
    idle_timeout: float = 180.0
    max_retries: int = 1  # one visible retry, only for transient errors before any output
    extra_body: dict[str, Any] = field(default_factory=dict)
    # CLI transports
    cli_path: str | None = None
    command: list[str] = field(default_factory=list)  # transport = "command": argv template
    model_args: list[str] = field(default_factory=list)  # appended when model != "default"
    effort_args: list[str] = field(default_factory=list)  # appended when an effort is set
    resume_args: list[str] = field(default_factory=list)  # appended to resume a native session
    prompt_via: str = "stdin"  # "stdin" | "arg" (argv element; never a shell string)
    output_format: str = "plain"  # "plain" | "json" | "jsonl"
    strip_env: list[str] = field(default_factory=list)  # env vars removed so the CLI can't bill an API key
    verified: bool = True  # False = preset built from docs but not exercised end-to-end
    web_search: bool = True  # let this lane search the web (where its transport supports it)
    enabled_by_default: bool = True
    aliases: list[str] = field(default_factory=list)

    @property
    def billing(self) -> Billing:
        if self.transport == "fake" or self.kind == "fake":
            return Billing.NONE
        return Billing.API if self.transport == "api" else Billing.SUBSCRIPTION


BUILTIN_PROVIDERS: list[ProviderConfig] = [
    ProviderConfig(
        id="claude", kind="anthropic", display_name="Claude", model="claude-opus-5-5",
        transport="claude-code", api_key_env=["ANTHROPIC_API_KEY"], max_tokens=16000, aliases=["anthropic"],
        strip_env=["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"],
    ),
    ProviderConfig(
        id="openai", kind="openai", display_name="OpenAI", model="default", api_model="gpt-5.5",
        transport="codex", api_key_env=["OPENAI_API_KEY"], aliases=["chatgpt", "gpt"],
        strip_env=["OPENAI_API_KEY", "CODEX_API_KEY"],
    ),
    ProviderConfig(
        id="gemini", kind="gemini", display_name="Gemini", model="default", api_model="gemini-3.7-flash",
        transport="command", command=["agy", "-p", "{prompt}"], prompt_via="arg", output_format="plain",
        api_key_env=["GEMINI_API_KEY", "GOOGLE_API_KEY"], aliases=["google"],
        strip_env=["GEMINI_API_KEY", "GOOGLE_API_KEY"], verified=False,
        enabled_by_default=False,  # off unless you `cockpit unmute gemini` in a chat
    ),
    ProviderConfig(
        id="grok", kind="xai", display_name="Grok", model="default", api_model="grok-4.6",
        transport="grok", api_key_env=["XAI_API_KEY"], aliases=["xai"], strip_env=["XAI_API_KEY"],
    ),
    ProviderConfig(
        id="perplexity", kind="perplexity", display_name="Perplexity", model="default", api_model="sonar-pro",
        transport="manual", api_key_env=["PERPLEXITY_API_KEY", "PPLX_API_KEY"], aliases=["pplx", "sonar"],
    ),
]

NOT_ALIASES = {
    "codex": (
        "`codex` is a transport, not a provider: the `openai` lane already runs through the Codex CLI "
        "(subscription-backed). Note Codex is a coding agent, so answers can differ from ChatGPT."
    ),
}


@dataclass
class Pricing:
    input_per_mtok: float
    output_per_mtok: float


@dataclass
class ApiDefaults:
    enabled: bool = False
    monthly_limit_usd: float = 0.0
    mode: str = "per-run"  # "per-run" | "until-disabled"


@dataclass
class CockpitConfig:
    home: Path
    config_path: Path
    db_path: Path
    providers: dict[str, ProviderConfig]
    judge: str = "claude"
    system_prompt: str | None = None  # visible global instruction; none by default
    min_responses_for_analysis: int = 2
    pricing: dict[str, Pricing] = field(default_factory=dict)
    api: ApiDefaults = field(default_factory=ApiDefaults)
    demo: bool = False

    @property
    def provider_ids(self) -> list[str]:
        return list(self.providers)

    def resolve_provider(self, name: str) -> str:
        key = name.strip().lower()
        if key in self.providers:
            return key
        for pid, p in self.providers.items():
            if key in (a.lower() for a in p.aliases) or key == p.display_name.lower():
                return pid
        if key in NOT_ALIASES:
            raise CockpitError(f"{name!r}: {NOT_ALIASES[key]}")
        names = list(self.providers) + [a for p in self.providers.values() for a in p.aliases]
        close = difflib.get_close_matches(key, names, n=1)
        hint = f" Did you mean {close[0]!r}?" if close else ""
        raise CockpitError(f"unknown provider {name!r}.{hint} Known: {', '.join(self.providers)}")

    def resolve_many(self, spec: str | list[str] | None) -> list[str]:
        if not spec:
            return []
        items = spec.split(",") if isinstance(spec, str) else [x for s in spec for x in s.split(",")]
        out: list[str] = []
        for item in items:
            if item.strip():
                pid = self.resolve_provider(item)
                if pid not in out:
                    out.append(pid)
        return out

    def system_prompt_for(self, pid: str) -> str | None:
        p = self.providers[pid]
        return p.system_prompt if p.system_prompt is not None else self.system_prompt

    def workdir(self, pid: str) -> Path:
        """Empty, dedicated working directory for agent CLIs (never the user's repo)."""
        return self.home / "workspaces" / pid


def default_home() -> tuple[Path, Path]:
    if os.environ.get("COCKPIT_HOME"):
        h = Path(os.environ["COCKPIT_HOME"]).expanduser()
        return h, h
    cfg = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser() / "cockpit"
    data = Path(os.environ.get("XDG_DATA_HOME", "~/.local/share")).expanduser() / "cockpit"
    return cfg, data


_PROVIDER_FIELDS = {f.name for f in fields(ProviderConfig)} - {"id"}


def load_config(config_path: Path | None = None, *, demo: bool = False, home: Path | None = None) -> CockpitConfig:
    if home is not None:
        cfg_dir = data_dir = home
    else:
        cfg_dir, data_dir = default_home()
    path = config_path or (cfg_dir / "config.toml")
    raw: dict[str, Any] = {}
    if path.exists():
        try:
            raw = tomllib.loads(path.read_text())
        except tomllib.TOMLDecodeError as e:
            raise CockpitError(f"{path}: invalid TOML: {e}") from e

    providers = {
        p.id: replace(p, api_key_env=list(p.api_key_env), aliases=list(p.aliases), command=list(p.command),
                      resume_args=list(p.resume_args), strip_env=list(p.strip_env))
        for p in BUILTIN_PROVIDERS
    }
    defaults = raw.get("defaults", {})
    for key in ("timeout", "idle_timeout", "max_retries"):
        if key in defaults:
            for p in providers.values():
                setattr(p, key, defaults[key])

    for pid, spec in (raw.get("providers") or {}).items():
        pid = pid.lower()
        spec = dict(spec)
        if "reasoning" in spec:  # v1 name
            spec["effort"] = spec.pop("reasoning")
        unknown = set(spec) - _PROVIDER_FIELDS
        if unknown:
            raise CockpitError(f"{path}: [providers.{pid}] unknown keys: {', '.join(sorted(unknown))}")
        if "transport" in spec:
            spec["transport"] = TRANSPORT_ALIASES.get(spec["transport"], spec["transport"])
            if spec["transport"] != "command" and pid in providers and "command" not in spec:
                spec.setdefault("verified", True)
        if pid in providers:
            providers[pid] = replace(providers[pid], **spec)
        else:
            if "kind" not in spec or "model" not in spec:
                raise CockpitError(f"{path}: new provider [providers.{pid}] needs `kind` and `model`")
            spec = {"display_name": pid.capitalize(), **spec}
            providers[pid] = ProviderConfig(id=pid, **spec)
        p = providers[pid]
        if isinstance(p.api_key_env, str):
            p.api_key_env = [p.api_key_env]
        if p.kind not in KINDS:
            raise CockpitError(f"{path}: [providers.{pid}] kind must be one of {KINDS}")
        if p.transport not in TRANSPORTS:
            raise CockpitError(f"{path}: [providers.{pid}] transport must be one of {TRANSPORTS}")
        if p.prompt_via not in ("stdin", "arg") or p.output_format not in ("plain", "json", "jsonl"):
            raise CockpitError(f"{path}: [providers.{pid}] prompt_via must be stdin|arg, output_format plain|json|jsonl")
        if p.transport == "command" and not p.command:
            raise CockpitError(f"{path}: [providers.{pid}] transport = \"command\" needs a `command` argv list")

    for pid in [p.lower() for p in raw.get("disabled_providers", [])]:
        providers.pop(pid, None)

    if demo:
        for p in providers.values():
            p.transport, p.verified = "fake", True

    pricing = {
        model: Pricing(float(v["input_per_mtok"]), float(v["output_per_mtok"]))
        for model, v in (raw.get("pricing") or {}).items()
    }
    api_raw = raw.get("api") or {}
    api = ApiDefaults(
        enabled=bool(api_raw.get("enabled", False)),
        monthly_limit_usd=float(api_raw.get("monthly_limit_usd", 0)),
        mode=str(api_raw.get("mode", "per-run")),
    )
    if api.mode not in ("per-run", "until-disabled"):
        raise CockpitError(f"{path}: [api] mode must be per-run or until-disabled")
    cfg = CockpitConfig(
        home=data_dir, config_path=path, db_path=data_dir / ("demo.db" if demo else "cockpit.db"),
        providers=providers, judge=str(defaults.get("judge", "claude")),
        system_prompt=defaults.get("system_prompt") or None,
        min_responses_for_analysis=int(defaults.get("min_responses_for_analysis", 2)),
        pricing=pricing, api=api, demo=demo,
    )
    if cfg.judge not in providers:
        cfg.judge = next(iter(providers), "claude")
    return cfg


CONFIG_TEMPLATE = """\
# LLM Cockpit configuration. Subscription-first: lanes use each vendor's official CLI where one exists.
# API keys are never stored here or in the database.

[defaults]
judge = "claude"             # default judge / analyzer / synthesizer (switch any time with --judge)
timeout = 600                # seconds per lane run
idle_timeout = 180           # seconds with no output before a run is treated as stalled
max_retries = 1              # one visible retry, only for transient errors before any output
# system_prompt = ""         # VISIBLE instruction sent identically to every lane (none by default)

# Paid API use is OFF until you turn it on. Nothing ever falls back to the API silently.
[api]
enabled = false
monthly_limit_usd = 0        # hard cap; must be > 0 for any API run
mode = "per-run"             # "per-run": each API run needs --allow-api; "until-disabled"

# Web search is ON for every lane that supports it (Claude, ChatGPT/Codex, Grok).
# Turn it off for one lane with `web_search = false` in that lane's section.

[providers.claude]           # Claude Code CLI, signed in with your Claude subscription (`claude` then /login)
transport = "claude-code"
model = "claude-opus-5-5"
# effort = "high"            # low | medium | high | xhigh | max

[providers.openai]           # Codex CLI signed in with ChatGPT (`codex login`). Codex is a coding agent.
transport = "codex"
model = "default"            # let Codex use its current default model (it records which one it used)
# effort = "high"            # Codex model_reasoning_effort

[providers.gemini]           # Antigravity CLI (`agy`), Google AI Pro/Ultra sign-in. UNVERIFIED preset. Off by default.
enabled_by_default = false
transport = "command"
command = ["agy", "-p", "{prompt}"]
prompt_via = "arg"
output_format = "plain"
model = "default"

[providers.grok]             # Grok Build CLI (`grok login`), SuperGrok / grok.com sign-in. Verified on grok 1.0.77.
transport = "grok"
model = "default"            # let Grok use its current default model (it reports which one it used)
# effort = "high"            # --reasoning-effort

[providers.perplexity]       # No subscription CLI exists. "manual" = one-click helper: Cockpit opens Perplexity in
transport = "manual"         # your browser with the question; you copy the answer back. Uses your Pro plan, no API cost.
# transport = "api"          # alternative: paid Sonar API (needs [api] enabled + a [pricing] entry); verify their
# model = "sonar-pro"        # current API first - Perplexity has been moving from Sonar to its Agent API.

# Needed to enforce the monthly API limit (USD per million tokens). Check current prices.
# [pricing."sonar-pro"]
# input_per_mtok = 0.0
# output_per_mtok = 0.0
"""
