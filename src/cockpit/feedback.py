"""Editable feedback packets: what selected lanes see from the rest of the panel.

A packet is plain text the user reviews and can edit before anything is sent.
Recipients receive EXACTLY the stored packet text (+ the user's next
instruction). Disclosure levels:

  summary     agreements (+why), disagreements by type, open questions, challenge
  anonymized  summary + the strongest anonymized argument for each side   (default)
  selected    summary + chosen full responses (relabelled "Analysis 1..n")
  everything  summary + every full response
  custom      only what the user writes

No provider names or judge labels are ever rendered.
"""

from __future__ import annotations

from typing import Any

from .analysis import sanitize_feedback
from .domain import Disclosure
from .store import Run

ANTI_CONVERGENCE = """\
HOW TO USE THIS FEEDBACK
Do not treat the current panel view as correct merely because multiple analyses agree.
Re-evaluate the assumptions, evidence, and reasoning independently.
Identify the strongest credible argument against the current working conclusion.
State:
1. what you still agree with,
2. what you now disagree with,
3. what changed your view,
4. what remains uncertain,
5. what evidence would change your conclusion."""

DEFAULT_INSTRUCTION = "Give your revised answer in full."

HEADER = ("FEEDBACK FROM AN INDEPENDENT REVIEW\n"
          "Your previous answer was reviewed alongside other independent analyses of the same question. "
          "This is context, not truth.")


def _section(title: str, lines: list[str]) -> str:
    lines = [ln for ln in lines if ln and ln.strip()]
    return f"{title}\n" + "\n".join(lines) if lines else ""


def _evidence_note(ev: dict[str, Any] | None) -> str:
    if not ev or ev.get("responses", 0) < 2:
        return ""
    if ev.get("cited", 0) == 0:
        return " [no sources cited]"
    if ev.get("independent_sources") == 1:
        return " [rests on one shared source]"
    return ""


def render_packet(
    result: dict[str, Any] | None,
    disclosure: Disclosure,
    labels: list[str],
    full_responses: list[Run] | None = None,
) -> str:
    if disclosure == Disclosure.CUSTOM or result is None:
        return f"{HEADER}\n\n(write your feedback here)\n\n{ANTI_CONVERGENCE}"

    def clean(x: Any) -> str:
        return sanitize_feedback(str(x or ""), labels)[0]

    parts = [HEADER]
    if result.get("summary"):
        parts.append(_section("CONTEXT SUMMARY", [clean(result["summary"])]))
    parts.append(_section("CURRENT AGREEMENTS (and why they are currently agreed)", [
        f"- {clean(a.get('point'))}" + (f" — {clean(a.get('why'))}" if a.get("why") else "")
        + (f" (depends on: {clean(a.get('depends_on'))})" if a.get("depends_on") else "") + _evidence_note(a.get("_evidence"))
        for a in result.get("agreements", []) if isinstance(a, dict)
    ]))
    dis_lines: list[str] = []
    for d in result.get("disagreements", []):
        if not isinstance(d, dict):
            continue
        dis_lines.append(f"- {clean(d.get('topic'))} [{d.get('type', 'unclassified')} disagreement]")
        if disclosure in (Disclosure.ANONYMIZED, Disclosure.SELECTED, Disclosure.EVERYTHING):
            for i, side in enumerate([s for s in d.get("sides", []) if isinstance(s, dict)], 1):
                dis_lines.append(f"    Side {i}: {clean(side.get('position'))}{_evidence_note(side.get('_evidence'))}")
                for key, label in (("reasoning", "reasoning"), ("evidence", "evidence"), ("assumptions", "assumes")):
                    if side.get(key):
                        dis_lines.append(f"      {label}: {clean(side[key])}")
    parts.append(_section("IMPORTANT DISAGREEMENTS (and the logic behind each side)", dis_lines))
    parts.append(_section("ASSUMPTIONS DRIVING DISAGREEMENT", [f"- {clean(x)}" for x in result.get("assumptions_driving_divergence", [])]))
    parts.append(_section("FACTUAL CONFLICTS", [
        f"- {clean(f.get('claim'))}" + (f" — {clean(f.get('note'))}" if f.get("note") else "")
        for f in result.get("factual_conflicts", []) if isinstance(f, dict)]))
    parts.append(_section("WEAK OR UNSUPPORTED CLAIMS", [
        f"- {clean(u.get('claim'))}" + (f" — {clean(u.get('why'))}" if u.get("why") else "")
        for u in result.get("unsupported_claims", []) if isinstance(u, dict)]))
    parts.append(_section("IMPORTANT UNIQUE INSIGHTS", [
        f"- {clean(u.get('insight'))}" for u in result.get("unique_insights", []) if isinstance(u, dict)]))
    parts.append(_section("UNRESOLVED QUESTIONS", [
        f"- {clean(v.get('question'))}" for v in result.get("verification_needed", []) if isinstance(v, dict)]
        + [f"- (not covered by anyone) {clean(o)}" for o in result.get("omissions", [])]))
    ch = result.get("challenge") or {}
    parts.append(_section("STRONGEST CHALLENGE TO THE CURRENT CONSENSUS", [
        f"Current consensus: {clean(ch.get('current_consensus'))}" if ch.get("current_consensus") else "",
        f"Strongest case against it: {clean(ch.get('strongest_case_against'))}" if ch.get("strongest_case_against") else "",
        f"What would falsify it: {clean(ch.get('what_would_falsify'))}" if ch.get("what_would_falsify") else "",
        *[f"Untested assumption: {clean(u)}" for u in ch.get("untested_assumptions", [])],
    ]))
    parts.append(_section("WHAT SHOULD BE RECONSIDERED NEXT", [f"- {clean(x)}" for x in result.get("next_challenges", [])]))
    if disclosure in (Disclosure.SELECTED, Disclosure.EVERYTHING) and full_responses:
        for i, run in enumerate(full_responses, 1):
            parts.append(f"FULL RESPONSE — Analysis {i}\n<analysis>\n{run.output_text or ''}\n</analysis>")
    parts.append(ANTI_CONVERGENCE)
    return "\n\n".join(p for p in parts if p)


def compose_message(content: str, instruction: str | None) -> str:
    """Exactly what each recipient lane receives."""
    return f"{content.rstrip()}\n\nUSER'S NEXT INSTRUCTION\n{(instruction or DEFAULT_INSTRUCTION).strip()}"
