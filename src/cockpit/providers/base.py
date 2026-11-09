"""Provider adapter contract.

An adapter turns a provider-neutral ``ChatRequest`` (full canonical history +
settings) into a stream of normalized ``AdapterEvent`` objects. All
provider-specific wire formats, auth headers and error shapes stay inside the
adapter modules.

Cancellation is cooperative asyncio cancellation: cancelling the task that is
iterating ``stream()`` closes the HTTP stream / kills the subprocess.
"""

from __future__ import annotations

import abc
import enum
import json
import re
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, ClassVar

import httpx

from ..config import ProviderConfig
from ..secrets import SecretResolver, redact
from ..domain import ChatMessage

# --------------------------------------------------------------------------
# Requests and events
# --------------------------------------------------------------------------


@dataclass
class ChatRequest:
    messages: list[ChatMessage]  # canonical history, ending with the new user turn
    model: str
    system_prompt: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    reasoning: str | None = None
    extra_body: dict[str, Any] = field(default_factory=dict)
    # Optimization hint for transports with native sessions (e.g. CLI). When set,
    # the native session is known to already contain messages[:-1].
    native_session_id: str | None = None
    # Stateless one-off call (judges): transports must not persist a native session.
    ephemeral: bool = False
    # Let the model search the web (only honoured by transports with the "web_search" capability).
    web_search: bool = False


@dataclass
class TextDelta:
    text: str


@dataclass
class NativeSession:
    session_id: str


@dataclass
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None  # only when the provider reports it


@dataclass
class ResponseMeta:
    model: str | None = None
    response_id: str | None = None
    finish_reason: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)  # e.g. citations


AdapterEvent = TextDelta | NativeSession | Usage | ResponseMeta


class ErrorKind(str, enum.Enum):
    NOT_CONFIGURED = "not_configured"  # missing API key / config
    NOT_INSTALLED = "not_installed"  # CLI binary missing
    AUTH = "auth"
    RATE_LIMIT = "rate_limit"
    MODEL_UNAVAILABLE = "model_unavailable"
    BAD_REQUEST = "bad_request"
    CONTENT_BLOCKED = "content_blocked"
    SERVER = "server"
    NETWORK = "network"
    TIMEOUT = "timeout"
    PROTOCOL = "protocol"  # stream format we could not understand
    NATIVE_SESSION_LOST = "native_session_lost"
    INTERNAL = "internal"


class ProviderError(Exception):
    def __init__(
        self,
        kind: ErrorKind,
        message: str,
        *,
        retryable: bool = False,
        status: int | None = None,
    ) -> None:
        super().__init__(redact(message))
        self.kind = kind
        self.retryable = retryable
        self.status = status

    @property
    def message(self) -> str:
        return str(self)


@dataclass
class HealthReport:
    provider: str
    transport: str
    model: str
    installed: bool | None = None  # None = not applicable (API transports)
    credentials: bool | None = None
    authenticated: bool | None = None  # None = not checked
    model_available: bool | None = None  # None = could not verify
    notes: list[str] = field(default_factory=list)
    remediation: list[str] = field(default_factory=list)
    suggested_models: list[str] = field(default_factory=list)
    billing: str = "api"  # subscription | api | none
    subscription_path: bool | None = None  # True = verified to bill the subscription, not an API key
    billing_warnings: list[str] = field(default_factory=list)
    verified_preset: bool = True
    capabilities: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return (
            self.installed is not False
            and self.credentials is not False
            and self.authenticated is not False
            and self.model_available is not False
        )


# --------------------------------------------------------------------------
# Adapter base classes
# --------------------------------------------------------------------------

_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\-/@]{0,127}$")


def validate_model_name(model: str) -> str:
    """Model ids end up in URL paths and argv; reject anything odd."""
    if not _MODEL_RE.match(model or "") or ".." in model:
        raise ProviderError(ErrorKind.BAD_REQUEST, f"invalid model identifier: {model!r}")
    return model


# Capability names advertised by adapters; the UI disables unsupported controls per lane.
CAPABILITIES = (
    "streaming", "native_session", "fork", "files", "web_search", "reasoning_effort", "stop",
    "citations", "usage_status", "subscription_auth", "reports_cost",
)


class ProviderAdapter(abc.ABC):
    transport: ClassVar[str] = "api"
    capabilities: ClassVar[frozenset[str]] = frozenset({"streaming", "stop"})
    streaming_granularity: ClassVar[str] = "tokens"  # tokens | message | chunks

    def __init__(self, config: ProviderConfig, secrets: SecretResolver) -> None:
        self.config = config
        self.secrets = secrets

    @property
    def provider_id(self) -> str:
        return self.config.id

    @abc.abstractmethod
    def stream(self, request: ChatRequest) -> AsyncIterator[AdapterEvent]:
        """Yield normalized events; raise ProviderError on failure."""

    @abc.abstractmethod
    async def health_check(self, live: bool = False) -> HealthReport:
        """Check install/auth/model availability without paid inference unless live."""

    def postprocess(self, text: str) -> str:
        """Normalized text committed to the lane. The raw output is stored separately."""
        return text

    def client_version(self) -> str | None:
        return None

    def new_report(self) -> HealthReport:
        return HealthReport(
            self.provider_id, self.transport, self.config.model, billing=self.config.billing.value,
            verified_preset=self.config.verified, capabilities=sorted(self.capabilities),
        )

    async def live_ping(self, report: HealthReport) -> None:
        """Tiny real request; confirms auth + model end to end. Costs a few tokens."""
        req = ChatRequest(
            messages=[ChatMessage("user", "Reply with the single word OK.")],
            model=self.config.model,
            max_tokens=max(16, min(self.config.max_tokens or 64, 64)),
        )
        try:
            got = ""
            async for ev in self.stream(req):
                if isinstance(ev, TextDelta):
                    got += ev.text
            report.authenticated = True
            report.model_available = True
            report.notes.append(f"live ping ok ({got.strip()[:20]!r})")
        except ProviderError as e:
            if e.kind == ErrorKind.AUTH:
                report.authenticated = False
            elif e.kind == ErrorKind.MODEL_UNAVAILABLE:
                report.authenticated = True
                report.model_available = False
            report.notes.append(f"live ping failed: {e.kind.value}: {e.message[:200]}")


# --------------------------------------------------------------------------
# HTTP / SSE plumbing
# --------------------------------------------------------------------------


@dataclass
class SSEEvent:
    event: str | None
    data: str


class SSEDecoder:
    """Incremental decoder for text/event-stream, one line at a time."""

    def __init__(self) -> None:
        self._event: str | None = None
        self._data: list[str] = []

    def feed(self, line: str) -> SSEEvent | None:
        line = line.rstrip("\r\n")
        if line == "":
            if not self._data and self._event is None:
                return None
            ev = SSEEvent(self._event, "\n".join(self._data))
            self._event, self._data = None, []
            return ev
        if line.startswith(":"):
            return None
        name, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if name == "event":
            self._event = value
        elif name == "data":
            self._data.append(value)
        return None

    def flush(self) -> SSEEvent | None:
        return self.feed("")


def parse_json(data: str, provider: str) -> dict[str, Any]:
    try:
        obj = json.loads(data)
    except json.JSONDecodeError as e:
        raise ProviderError(ErrorKind.PROTOCOL, f"{provider}: unparseable stream chunk: {data[:200]!r}") from e
    if not isinstance(obj, dict):
        raise ProviderError(ErrorKind.PROTOCOL, f"{provider}: unexpected stream chunk: {data[:200]!r}")
    return obj


def classify_http_error(provider: str, status: int, body: str) -> ProviderError:
    detail = body.strip()
    try:
        obj = json.loads(body)
        err = obj.get("error", obj) if isinstance(obj, dict) else obj
        if isinstance(err, dict):
            detail = str(err.get("message") or err.get("detail") or err)
        elif isinstance(err, list) and err and isinstance(err[0], dict):  # Gemini sometimes wraps in a list
            inner = err[0].get("error", err[0])
            detail = str(inner.get("message", inner))
    except (json.JSONDecodeError, AttributeError):
        pass
    detail = detail[:500]
    msg = f"{provider}: HTTP {status}: {detail}"
    low = detail.lower()
    if status in (401, 403):
        return ProviderError(ErrorKind.AUTH, msg, status=status)
    if status == 429:
        return ProviderError(ErrorKind.RATE_LIMIT, msg, retryable=True, status=status)
    if status == 404 or ("model" in low and any(w in low for w in ("not found", "does not exist", "not supported", "invalid model", "unknown model"))):
        return ProviderError(ErrorKind.MODEL_UNAVAILABLE, msg, status=status)
    if status in (408,):
        return ProviderError(ErrorKind.TIMEOUT, msg, retryable=True, status=status)
    if status >= 500:
        return ProviderError(ErrorKind.SERVER, msg, retryable=True, status=status)
    if "api key" in low or "api_key" in low:
        return ProviderError(ErrorKind.AUTH, msg, status=status)
    return ProviderError(ErrorKind.BAD_REQUEST, msg, status=status)


class HTTPAdapter(ProviderAdapter):
    """Shared plumbing for adapters that speak HTTP + SSE."""

    default_base_url: ClassVar[str] = ""
    default_key_envs: ClassVar[tuple[str, ...]] = ()
    key_help_url: ClassVar[str] = ""

    # Tests inject an httpx.MockTransport here.
    http_transport: httpx.AsyncBaseTransport | None = None

    @property
    def base_url(self) -> str:
        return (self.config.base_url or self.default_base_url).rstrip("/")

    def key_envs(self) -> tuple[str, ...]:
        return tuple(self.config.api_key_env) or self.default_key_envs

    def api_key(self) -> str:
        key = self.secrets.resolve(self.provider_id, self.key_envs(), self.config.api_key_command)
        if not key:
            envs = " or ".join(self.key_envs()) or "an API key"
            raise ProviderError(ErrorKind.NOT_CONFIGURED, f"{self.provider_id}: no API key (set {envs})")
        return key

    def client(self) -> httpx.AsyncClient:
        timeout = httpx.Timeout(
            connect=15.0, read=self.config.idle_timeout, write=30.0, pool=15.0
        )
        return httpx.AsyncClient(timeout=timeout, transport=self.http_transport)

    async def post_sse(
        self, url: str, headers: dict[str, str], body: dict[str, Any]
    ) -> AsyncIterator[SSEEvent]:
        try:
            async with self.client() as client:
                async with client.stream("POST", url, headers=headers, json=body) as resp:
                    if resp.status_code >= 400:
                        raw = (await resp.aread()).decode("utf-8", "replace")
                        raise classify_http_error(self.provider_id, resp.status_code, raw)
                    decoder = SSEDecoder()
                    async for line in resp.aiter_lines():
                        ev = decoder.feed(line)
                        if ev is not None:
                            yield ev
                    tail = decoder.flush()
                    if tail is not None:
                        yield tail
        except ProviderError:
            raise
        except httpx.TimeoutException as e:
            raise ProviderError(
                ErrorKind.TIMEOUT, f"{self.provider_id}: no data for {self.config.idle_timeout:.0f}s ({type(e).__name__})", retryable=True
            ) from e
        except httpx.TransportError as e:
            raise ProviderError(ErrorKind.NETWORK, f"{self.provider_id}: network error: {type(e).__name__}: {e}", retryable=True) from e

    async def get_json(self, url: str, headers: dict[str, str], params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            async with self.client() as client:
                resp = await client.get(url, headers=headers, params=params)
        except httpx.TimeoutException as e:
            raise ProviderError(ErrorKind.TIMEOUT, f"{self.provider_id}: timeout", retryable=True) from e
        except httpx.TransportError as e:
            raise ProviderError(ErrorKind.NETWORK, f"{self.provider_id}: network error: {type(e).__name__}: {e}", retryable=True) from e
        if resp.status_code >= 400:
            raise classify_http_error(self.provider_id, resp.status_code, resp.text)
        try:
            return resp.json()
        except ValueError as e:
            raise ProviderError(ErrorKind.PROTOCOL, f"{self.provider_id}: non-JSON response") from e

    # --- health ---------------------------------------------------------

    def auth_headers(self, key: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {key}"}

    async def list_models(self, key: str) -> list[str] | None:
        """Return available model ids, or None if the provider has no list endpoint."""
        return None

    async def health_check(self, live: bool = False) -> HealthReport:
        report = self.new_report()
        try:
            validate_model_name(self.config.model)
        except ProviderError as e:
            report.model_available = False
            report.notes.append(e.message)
            return report
        key = self.secrets.resolve(self.provider_id, self.key_envs(), self.config.api_key_command)
        report.credentials = bool(key)
        if not key:
            envs = " or ".join(self.key_envs())
            report.remediation.append(
                f"export {self.key_envs()[0] if self.key_envs() else 'API_KEY'}=...  (checked: {envs})"
                + (f"; get a key at {self.key_help_url}" if self.key_help_url else "")
            )
            return report
        try:
            models = await self.list_models(key)
        except ProviderError as e:
            if e.kind == ErrorKind.AUTH:
                report.authenticated = False
                report.remediation.append("API key was rejected - regenerate it and re-export")
            report.notes.append(f"{e.kind.value}: {e.message[:200]}")
            return report
        if models is None:
            report.notes.append("provider has no model-list endpoint; use `doctor --live` to verify")
        else:
            report.authenticated = True
            report.model_available = self.config.model in models
            if not report.model_available:
                report.suggested_models = suggest_models(self.config.model, models)
                report.remediation.append(
                    f"model {self.config.model!r} not offered to this key; set one with "
                    f"`cockpit model {self.provider_id} <id>` or in config.toml"
                )
        if live and report.authenticated is not False:
            await self.live_ping(report)
        return report


def suggest_models(wanted: str, available: list[str], limit: int = 6) -> list[str]:
    stem = re.split(r"[-.:]", wanted)[0].lower()
    close = [m for m in available if m.lower().startswith(stem)]
    return sorted(close or available, reverse=True)[:limit]


# --------------------------------------------------------------------------
# Generic SSE adapter skeleton
# --------------------------------------------------------------------------


class StreamParser(abc.ABC):
    """Stateful translator from SSE events to AdapterEvents for one response."""

    provider: str = "provider"

    def __init__(self, provider: str) -> None:
        self.provider = provider
        self.done = False
        self.saw_text = False

    @abc.abstractmethod
    def feed(self, sse: SSEEvent) -> list[AdapterEvent]: ...

    def finish(self) -> None:
        if not self.done:
            raise ProviderError(
                ErrorKind.PROTOCOL,
                f"{self.provider}: stream ended without a completion marker "
                "(connection dropped, or the provider changed its stream format)",
                retryable=not self.saw_text,
            )

    def _text(self, text: str) -> list[AdapterEvent]:
        if text:
            self.saw_text = True
            return [TextDelta(text)]
        return []


class SSEAdapter(HTTPAdapter):
    @abc.abstractmethod
    def build(self, request: ChatRequest, key: str) -> tuple[str, dict[str, str], dict[str, Any]]: ...

    @abc.abstractmethod
    def new_parser(self) -> StreamParser: ...

    async def stream(self, request: ChatRequest) -> AsyncIterator[AdapterEvent]:
        validate_model_name(request.model)
        key = self.api_key()
        url, headers, body = self.build(request, key)
        parser = self.new_parser()
        async for sse in self.post_sse(url, headers, body):
            for ev in parser.feed(sse):
                yield ev
        parser.finish()


def deep_merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out
