"""`cockpit doctor`: runtime, database, each lane's client, sign-in, billing path, models."""

from __future__ import annotations

import asyncio
import os
import stat
import sys
from typing import Any

from rich.markup import escape
from rich.table import Table

from . import billing
from .config import CockpitConfig
from .engine import Cockpit
from .providers.base import HealthReport, ProviderError
from .store import Store


def _mark(v: bool | None, yes: str = "yes", no: str = "NO", unknown: str = "?") -> str:
    if v is None:
        return f"[dim]{unknown}[/]"
    return f"[green]{yes}[/]" if v else f"[red]{no}[/]"


async def gather_reports(engine: Cockpit, providers: list[str], live: bool) -> list[HealthReport]:
    async def one(pid: str) -> HealthReport:
        try:
            return await asyncio.wait_for(engine.adapter(pid).health_check(live=live), timeout=90)
        except (ProviderError, asyncio.TimeoutError, Exception) as e:  # noqa: BLE001 - doctor never crashes
            r = engine.adapter(pid).new_report()
            r.notes.append(f"health check failed: {type(e).__name__}: {e}")
            return r

    return list(await asyncio.gather(*(one(p) for p in providers)))


def environment_checks(config: CockpitConfig, store: Store) -> list[tuple[str, bool, str]]:
    out = [("runtime", sys.version_info >= (3, 11), f"Python {sys.version.split()[0]}")]
    out.append(("config", True, f"{config.config_path} ({'found' if config.config_path.exists() else 'defaults; `cockpit init`'})"))
    db = config.db_path
    try:
        store.set_meta("doctor_probe", "1")
        writable = True
    except Exception:  # noqa: BLE001
        writable = False
    mode = stat.S_IMODE(os.stat(db).st_mode) if db.exists() else 0o600
    out.append(("database", writable and mode & 0o077 == 0,
                f"{db} ({'writable' if writable else 'NOT writable'}, mode {oct(mode)}"
                + ("" if mode & 0o077 == 0 else " - readable by others! chmod 600") + ")"))
    pol = billing.policy(config, store)
    out.append(("API spend", True, f"{'ON' if pol.enabled else 'off'} · limit ${pol.monthly_limit_usd:.2f}/month · "
                                   f"spent ${store.api_spend():.2f} this month"))
    if config.demo:
        out.append(("mode", True, "DEMO - fake lanes, separate demo database"))
    return out


def print_doctor(renderer: Any, config: CockpitConfig, store: Store, reports: list[HealthReport], live: bool) -> bool:
    con = renderer.console
    con.print("[bold]Cockpit[/]")
    for name, ok, detail in environment_checks(config, store):
        con.print(f"  {'[green]✓[/]' if ok else '[red]✕[/]'} {name:10} {escape(detail)}")
    t = Table(title="Lanes" + (" (live check)" if live else ""), title_justify="left")
    for c in ("lane", "transport", "billing", "client", "signed in", "subscription path", "model", "verified preset"):
        t.add_column(c)
    for r in reports:
        t.add_row(config.providers[r.provider].display_name, r.transport, r.billing,
                  _mark(r.installed, "installed", "missing", "n/a") if r.transport != "api" else _mark(r.credentials, "key set", "no key", "n/a"),
                  _mark(r.authenticated, unknown="not checked"),
                  _mark(r.subscription_path, "yes", "NO (API!)", "unknown") if r.billing == "subscription" else "[dim]n/a[/]",
                  f"{escape(r.model)} " + _mark(r.model_available, "✓", "✕", ""),
                  _mark(r.verified_preset, "yes", "NO"))
    con.print(t)
    all_ok = True
    for r in reports:
        if not r.ok or r.billing_warnings:
            all_ok = False
        lines = [f"[bold red]BILLING: {escape(w)}[/]" for w in r.billing_warnings] + [f"[dim]{escape(n)}[/]" for n in r.notes] \
            + [f"[yellow]fix: {escape(x)}[/]" for x in r.remediation]
        if r.billing == "api":
            lines.append("[dim]paid API lane: runs only when you approve them (`cockpit api enable --limit N`, then --allow-api)[/]")
        if r.suggested_models:
            lines.append(f"[dim]available models include: {escape(', '.join(r.suggested_models))}[/]")
        if lines:
            con.print(f"[bold]{config.providers[r.provider].display_name}[/]")
            for line in lines:
                con.print(f"  {line}")
    if not live:
        con.print("[dim]`cockpit doctor --live` sends one tiny real prompt per lane (uses a little allowance).[/]")
    return all_ok
