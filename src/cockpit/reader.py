"""Reader page: one HTML file per chat, laid out for reading instead of the terminal.

Answers are rendered the way a chat app shows them (headings, tables, code, math,
web images) and standalone pieces an AI writes (a web page, a diagram, a drawing,
a document) become artifact cards with a live preview.

Safety model: nothing a model wrote is ever inserted into this page as live HTML.
Markdown is rendered with raw HTML disabled and links restricted to http/https/mailto.
Artifacts are written to their own files and shown in sandboxed frames (scripts
allowed, but no access to this page, your files, or the network beyond three script
CDNs). SVG drawings are shown as images, where scripts cannot run.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import quote

from markdown_it import MarkdownIt

from .domain import RunStatus

CDN = "https://cdnjs.cloudflare.com/ajax/libs"
# (url, sha384) - pinned versions, checked by the browser before they run
HLJS_JS = (f"{CDN}/highlight.js/11.9.0/highlight.min.js", "sha384-F/bZzf7p3Joyp5psL90p/p89AZJsndkSoGwRpXcZhleCWhd8SnRuoYo4d0yirjJp")
HLJS_LIGHT = (f"{CDN}/highlight.js/11.9.0/styles/github.min.css", "sha384-eFTL69TLRZTkNfYZOLM+G04821K1qZao/4QLJbet1pP4tcF+fdXq/9CdqAbWRl/L")
HLJS_DARK = (f"{CDN}/highlight.js/11.9.0/styles/github-dark.min.css", "sha384-wH75j6z1lH97ZOpMOInqhgKzFkAInZPPSPlZpYKYTOqsaizPvhQZmAtLcPKXpLyH")
KATEX_JS = (f"{CDN}/KaTeX/0.16.9/katex.min.js", "sha384-XjKyOOlGwcjNTAIQHIpgOno0Hl1YQqzUOEleOLALmuqehneUG+vnGctmUb0ZY0l8")
KATEX_CSS = (f"{CDN}/KaTeX/0.16.9/katex.min.css", "sha384-n8MVd4RsNIU0tAv4ct0nTaAbDJwPJzDEaqSD1odI+WdtXRGWt2kTvGFasHpSy3SV")
MERMAID_JS = (f"{CDN}/mermaid/10.9.0/mermaid.min.js", "sha384-6F4Ibv/ylL12O35KFWTeGTHuBKDz5L6yjKsgv3QHQ8s4NTqlDXq7kMlYXGs7MHFc")

SCRIPT_CDNS = "https://cdnjs.cloudflare.com https://cdn.jsdelivr.net https://unpkg.com"
# What an artifact may do inside its sandboxed frame: run its own scripts and load libraries
# from three CDNs. It may not call out to the network (connect-src 'none') or submit forms.
ARTIFACT_CSP = (
    f"default-src 'none'; script-src 'unsafe-inline' 'unsafe-eval' {SCRIPT_CDNS}; "
    f"style-src 'unsafe-inline' {SCRIPT_CDNS} https://fonts.googleapis.com; "
    f"font-src data: https://fonts.gstatic.com {SCRIPT_CDNS}; img-src https: data: blob:; "
    "media-src https: data: blob:; connect-src 'none'; form-action 'none'; base-uri 'none'"
)

KIND_LABEL = {"page": "Web page", "diagram": "Diagram", "drawing": "Drawing", "document": "Document"}
COLORS = {"claude": "#c15f3c", "openai": "#0f8a6c", "grok": "#5b5bd6", "perplexity": "#1a8a9c", "gemini": "#2f6fde"}
_SAFE_LINK = re.compile(r"^(https?:|mailto:|#)", re.I)
_SAFE_IMG = re.compile(r"^(https:|data:image/(png|jpeg|gif|webp);)", re.I)


def esc(x: Any) -> str:
    return html.escape(str(x if x is not None else ""), quote=True)


# ----------------------------------------------------------------------
# Artifacts
# ----------------------------------------------------------------------


class Artifacts:
    """Collects the standalone pieces found in one answer and writes each to its own file."""

    def __init__(self, folder: Path | None, rel: str, prefix: str) -> None:
        self.folder, self.rel, self.prefix, self.n = folder, rel, prefix, 0
        self.files: list[Path] = []

    def save(self, ext: str, content: str) -> str | None:
        """Write one artifact file; returns the URL to use from the reader page."""
        if self.folder is None:
            return None
        from .cli import write_private

        self.n += 1
        name = f"{self.prefix}-{self.n}.{ext}"
        write_private(self.folder / name, content)
        self.files.append(self.folder / name)
        return f"{self.rel}/{quote(name)}"


def artifact_kind(lang: str, info: str, code: str, depth: int = 0) -> str | None:
    lines = code.count("\n") + 1
    titled = "title=" in info
    head = code.lstrip()[:200].lower()
    if lang == "mermaid":
        return "diagram"
    if lang == "svg" or (lang in ("xml", "html", "") and head.startswith(("<svg", "<?xml")) and "<svg" in code[:600].lower()):
        return "drawing"
    if lang in ("html", "htm") and (titled or lines >= 15 or re.search(r"<!doctype|<html|<body|<script|<canvas", code, re.I)):
        return "page"
    if lang in ("markdown", "md") and depth == 0 and (titled or lines >= 12):
        return "document"
    return None


def artifact_title(kind: str, info: str, code: str) -> str:
    m = re.search(r'title="([^"]{1,120})"', info)
    if not m and kind in ("page", "drawing"):
        m = re.search(r"<title[^>]*>([^<]{1,120})</title>", code, re.I)
    if not m and kind == "diagram":
        m = re.search(r"^\s*(?:%%\s*title:|title:?)\s+(.{1,120})$", code, re.I | re.M)
    if not m and kind == "document":
        m = re.search(r"^#{1,3}\s+(.{1,120})$", code, re.M)
    return (m.group(1).strip() if m else "") or KIND_LABEL[kind]


# Added to every framed artifact: tells the reader page how tall the content is, so the frame fits it.
# The reader only ever reads a number from this message.
_FIT = ("<script>(function(){function r(){try{var h=document.documentElement.getBoundingClientRect().height||"
        "document.body.scrollHeight;parent.postMessage({cockpitArtifactHeight:Math.ceil(h)},'*')}catch(e){}}"
        "addEventListener('load',function(){r();setTimeout(r,400);setTimeout(r,1500)});addEventListener('resize',r);"
        "if(window.ResizeObserver){new ResizeObserver(r).observe(document.documentElement)}})();</script>")


def sandbox_page(code: str) -> str:
    """The AI's page plus a policy that keeps it from calling out to the network."""
    meta = f'<meta http-equiv="Content-Security-Policy" content="{esc(ARTIFACT_CSP)}">'
    if re.search(r"</body\s*>", code, re.I):
        code = re.sub(r"(</body\s*>)", lambda m: _FIT + m.group(1), code, count=1, flags=re.I)
    else:
        code = code + _FIT
    if re.search(r"<head[^>]*>", code, re.I):
        return re.sub(r"(<head[^>]*>)", lambda m: m.group(1) + meta, code, count=1, flags=re.I)
    if re.search(r"<html[^>]*>", code, re.I):
        return re.sub(r"(<html[^>]*>)", lambda m: m.group(1) + f"<head><meta charset=\"utf-8\">{meta}</head>", code,
                      count=1, flags=re.I)
    return f'<!doctype html><html><head><meta charset="utf-8">{meta}</head><body>{code}</body></html>'


def mermaid_page(title: str, code: str) -> str:
    csp = (f"default-src 'none'; script-src 'unsafe-inline' {SCRIPT_CDNS}; style-src 'unsafe-inline'; "
           "img-src data:; font-src data:; connect-src 'none'; base-uri 'none'")
    src = json.dumps(code).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="{esc(csp)}"><title>{esc(title)}</title>
<style>body{{margin:0;padding:18px;font:16px system-ui,sans-serif;background:#fff;color:#1a1a1a}}
@media (prefers-color-scheme:dark){{body{{background:#1f1e1d;color:#eee}}}}
#d{{overflow-x:auto}} #d svg{{display:block;margin:0 auto;max-width:none!important}}
pre{{white-space:pre-wrap;margin:0;font:13px ui-monospace,Menlo,monospace}}</style></head>
<body><div id="d"><pre>{esc(code)}</pre></div>
<p id="wide" hidden style="margin:10px 0 0;font-size:13px;color:#777;text-align:center">This diagram is wider than the box: scroll sideways, or press Expand.</p>
<script src="{MERMAID_JS[0]}" integrity="{MERMAID_JS[1]}" crossorigin="anonymous"></script>
<script>(function(){{
var src={src},box=document.getElementById("d");
function fit(){{var svg=box.querySelector("svg");if(!svg||!svg.viewBox||!svg.viewBox.baseVal)return;
  var w=svg.viewBox.baseVal.width,h=svg.viewBox.baseVal.height,room=box.clientWidth;if(!w||!h)return;
  var k=w<=room?1:Math.max(room/w,0.72);  // shrink to fit, but never so far that the text is unreadable
  svg.setAttribute("width",Math.round(w*k));svg.setAttribute("height",Math.round(h*k));svg.style.height="auto";
  document.getElementById("wide").hidden=w*k<=room+2}}
if(!window.mermaid)return;  // offline: the diagram's source stays visible
mermaid.initialize({{startOnLoad:false,securityLevel:"strict",
  theme:matchMedia("(prefers-color-scheme: dark)").matches?"dark":"default",
  flowchart:{{useMaxWidth:false}},sequence:{{useMaxWidth:false}},themeVariables:{{fontSize:"16px"}}}});
mermaid.render("cockpit-diagram",src).then(function(r){{box.innerHTML=r.svg;fit();addEventListener("resize",fit)}})
  .catch(function(e){{var p=document.createElement("p");p.textContent="This diagram could not be drawn: "+(e&&e.message||e);
    p.style.color="#a8322d";box.parentNode.insertBefore(p,box)}});
}})();</script>{_FIT}</body></html>"""


def _card(kind: str, title: str, preview: str, lang: str, code: str) -> str:
    return (
        f'<figure class="artifact" data-kind="{kind}"><figcaption><span class="art-title">{esc(title)}</span>'
        f'<span class="art-kind">{KIND_LABEL[kind]}</span><span class="art-actions">'
        '<button type="button" data-tab="preview" class="on">Preview</button>'
        '<button type="button" data-tab="code">Code</button>'
        '<button type="button" data-copy="art">Copy</button>'
        '<button type="button" data-expand>Expand</button></span></figcaption>'
        f'<div class="art-preview">{preview}</div>'
        f'<div class="art-code" hidden><pre><code class="language-{esc(lang)}">{esc(code)}</code></pre></div></figure>'
    )


# ----------------------------------------------------------------------
# Markdown -> safe HTML
# ----------------------------------------------------------------------


def _fence(self: Any, tokens: Any, idx: int, options: Any, env: dict[str, Any]) -> str:
    tok = tokens[idx]
    info = (tok.info or "").strip()
    lang = re.sub(r"[^\w+#.-]", "", info.split()[0].lower()) if info else ""
    code = tok.content
    arts: Artifacts | None = env.get("artifacts")
    depth = int(env.get("depth", 0))
    kind = artifact_kind(lang, info, code, depth) if arts is not None else None
    if kind:
        title = artifact_title(kind, info, code)
        if kind == "document":
            arts.save("md", code)  # type: ignore[union-attr]
            inner = render_markdown(code, arts, depth + 1)
            return _card(kind, title, f'<div class="doc prose">{inner}</div>', "markdown", code)
        if kind == "drawing":
            url = arts.save("svg", code)  # type: ignore[union-attr]
            if url:
                return _card(kind, title, f'<img class="drawing" src="{esc(url)}" alt="{esc(title)}">', "xml", code)
        else:
            page = sandbox_page(code) if kind == "page" else mermaid_page(title, code)
            url = arts.save("html", page)  # type: ignore[union-attr]
            if kind == "diagram":
                arts.save("mmd", code)  # type: ignore[union-attr]
            if url:
                frame = (f'<iframe sandbox="allow-scripts" loading="lazy" referrerpolicy="no-referrer" '
                         f'src="{esc(url)}" title="{esc(title)}"></iframe>')
                return _card(kind, title, frame, "html" if kind == "page" else "plaintext", code)
    return (f'<div class="code"><div class="codebar"><span>{esc(lang or "text")}</span>'
            '<button type="button" data-copy="code">Copy</button></div>'
            f'<pre><code class="language-{esc(lang or "plaintext")}">{esc(code)}</code></pre></div>')


def _link_open(self: Any, tokens: Any, idx: int, options: Any, env: Any) -> str:
    tokens[idx].attrSet("target", "_blank")
    tokens[idx].attrSet("rel", "noopener noreferrer")
    return self.renderToken(tokens, idx, options, env)


def _image(self: Any, tokens: Any, idx: int, options: Any, env: Any) -> str:
    tok = tokens[idx]
    src = str(tok.attrGet("src") or "")
    alt = esc(tok.content or "")
    if not _SAFE_IMG.match(src):
        return f'<span class="noimg">[image: {alt}]</span>'
    return (f'<img src="{esc(src)}" alt="{alt}" loading="lazy" referrerpolicy="no-referrer"'
            + (f' title="{esc(tok.attrGet("title"))}"' if tok.attrGet("title") else "") + ">")


def _markdown() -> MarkdownIt:
    md = MarkdownIt("commonmark", {"html": False, "linkify": False}).enable(["table", "strikethrough"])
    md.validateLink = lambda url: bool(_SAFE_LINK.match(url.strip()) or _SAFE_IMG.match(url.strip()))  # type: ignore[method-assign]
    md.add_render_rule("fence", _fence)
    md.add_render_rule("link_open", _link_open)
    md.add_render_rule("image", _image)
    md.add_render_rule("table_open", lambda self, t, i, o, e: '<div class="tablewrap"><table>')
    md.add_render_rule("table_close", lambda self, t, i, o, e: "</table></div>")
    return md


_MD = _markdown()
_CODE_SPLIT = re.compile(r"(^[ \t]*```.*?^[ \t]*```[ \t]*$|^[ \t]*~~~.*?^[ \t]*~~~[ \t]*$|`[^`\n]+`)", re.S | re.M)
_MATH = re.compile(r"\$\$(.+?)\$\$|\\\[(.+?)\\\]|\\\((.+?)\\\)", re.S)


def render_markdown(text: str, artifacts: Artifacts | None = None, depth: int = 0) -> str:
    """Markdown as a chat app would show it. Raw HTML in the text is shown as text, never run."""
    maths: list[tuple[str, bool]] = []

    def hold(m: re.Match[str]) -> str:
        display = m.group(1) or m.group(2)
        maths.append(((display or m.group(3)).strip(), bool(display)))
        return f"COCKPITMATH{len(maths) - 1}X"

    parts = _CODE_SPLIT.split(text or "")
    for i in range(0, len(parts), 2):  # even parts are outside code
        parts[i] = _MATH.sub(hold, parts[i])
    out = _MD.render("".join(parts), {"artifacts": artifacts, "depth": depth})

    def put(m: re.Match[str]) -> str:
        tex, display = maths[int(m.group(1))]
        return f'<span class="math{" display" if display else ""}" data-tex="{esc(tex)}">{esc(tex)}</span>'

    return re.sub(r"COCKPITMATH(\d+)X", put, out)


def inline(text: Any) -> str:
    return _MD.renderInline(str(text or ""))


# ----------------------------------------------------------------------
# The page
# ----------------------------------------------------------------------


def _who(item: dict[str, Any], names: dict[str, tuple[str, str]]) -> str:
    refs = item.get("responses") or ([item["response"]] if item.get("response") else [])
    chips = []
    for ref in refs:
        pid, name = names.get(str(ref).strip().upper(), ("", str(ref)))
        chips.append(f'<span class="chip" style="--ai:{COLORS.get(pid, "#777")}">{esc(name)}</span>')
    return f'<span class="who">{"".join(chips)}</span>' if chips else ""


_VERBS = ("recommends|says|said|cites|cited|gives|gave|argues|claims|is|was|has|had|uses|used|omits|does|did|also|alone|notes|"
          "adds|assumes|treats|estimates|states|suggests|prefers|provides|includes|lists|mentions|warns|puts|quotes|"
          "reports|shows|offers|calls|frames|relies|goes|never|only|didn't|doesn't|did not|does not|and|or|vs|versus")


_NOT_A_LABEL = ("Plan", "Option", "Vitamin", "Series", "Type", "Class", "Part", "Schedule", "Tier", "Grade", "Phase",
                "Category", "Scenario", "Appendix", "Exhibit", "Table", "Figure", "Section", "Step", "Level", "Route",
                "Path", "Hepatitis", "Group", "Model", "Vitamin", "plan", "option", "route", "scenario", "step")


def name_labels(text_html: str, names: dict[str, tuple[str, str]]) -> str:
    """The judge writes about 'A', 'B', 'C' (it never knew who wrote what). Show the reader the AI's name instead.
    'A' is also an ordinary word, so it is only replaced where it clearly means the response."""
    if not names:
        return text_html

    def swap(m: re.Match[str]) -> str:
        pid, name = names[m.group(1)]
        return f'<b class="lbl" style="--ai:{COLORS.get(pid, "#777")}">{esc(name)}</b>'

    labels = "".join(sorted(names))
    others = labels.replace("A", "")
    parts = re.split(r"(<[^>]+>)", text_html)  # never touch the tags themselves
    if others:
        # "Plan B", "Option C", "Vitamin C" ... are not responses
        guard = "".join(f"(?<!{w} )" for w in _NOT_A_LABEL)
        pat = re.compile(rf"(?<![\w&;/'’-]){guard}(?:Response |Analysis )?([{others}])(?![\w-])(?!\.\w)")
        for i in range(0, len(parts), 2):
            parts[i] = pat.sub(swap, parts[i])
    out = "".join(parts)
    if "A" in names:
        a = lambda m: swap(re.match("(A)", "A"))  # type: ignore[arg-type]  # noqa: E731
        out = re.sub(r"(?<![\w&;/'’\"=-])(?:Response |Analysis )A(?![\w-])", a, out)
        out = re.sub(rf"(?<![\w&;/'’\"=-])A(?=(?:'s|’s)\b| (?:{_VERBS})\b|, (?:<b class=\"lbl\"|[B-Z]\b)| <b class=\"lbl\")", a, out)
        out = re.sub(r"(?<=</b> and )A(?![\w-])|(?<=</b>, )A(?=[ ,])|(?<=\()A(?= \d)", a, out)
    return out


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[a-z0-9\)\]%][.!?])\s+(?=[A-Z])", " ".join(str(text or "").split()))
    return [p for p in parts if p]


def _ul(items: list[str]) -> str:
    items = [i for i in items if i]
    return "<ul>" + "".join(f"<li>{i}</li>" for i in items) + "</ul>" if items else ""


def glance_html(a: Any, names: dict[str, tuple[str, str]], judge: str) -> str:
    """The comparison as an easy-to-scan box. `names` maps blind labels to (lane id, display name)."""
    res = a.result or {}
    if a.kind == "synthesis":
        return (f'<section class="glance synth"><h3>Synthesis <small>written by {esc(judge)}</small></h3>'
                f'<div class="prose">{render_markdown(str(res.get("synthesis", "")))}</div></section>')
    out: list[str] = []

    def block(css: str, title: str, body: str) -> None:
        if body:
            out.append(f'<div class="g {css}"><h4>{title}</h4>{body}</div>')

    warn = [f"<li>{inline(r)}</li>" for r in res.get("_consensus_risks", [])]
    if res.get("_missing_challenge"):
        warn.append("<li>The judge left out the challenge section, so treat this comparison as incomplete.</li>")
    summary = _sentences(res.get("summary", ""))
    if len(summary) > 2:
        out.append('<ul class="g-summary">' + "".join(f"<li>{inline(x)}</li>" for x in summary) + "</ul>")
    elif summary:
        out.append(f'<p class="g-summary">{inline(" ".join(summary))}</p>')
    block("warn", "Be careful", "<ul>" + "".join(warn) + "</ul>" if warn else "")
    block("agree", "Where they agree", _ul([
        f'{inline(x.get("point"))} {_who(x, names)}' + (f'<div class="sub">Why: {inline(x["why"])}</div>' if x.get("why") else "")
        + (f'<div class="sub">Depends on: {inline(x["depends_on"])}</div>' if x.get("depends_on") else "")
        for x in res.get("agreements", []) if isinstance(x, dict)]))
    dis = []
    for d in res.get("disagreements", []):
        if not isinstance(d, dict):
            continue
        sides = "".join(
            f'<div class="side"><div class="pos">{inline(s.get("position"))} {_who(s, names)}</div>'
            + "".join(f'<div class="sub"><b>{label}:</b> {inline(s[k])}</div>'
                      for k, label in (("reasoning", "Reasoning"), ("evidence", "Evidence"), ("assumptions", "Assumes")) if s.get(k))
            + "</div>" for s in d.get("sides", []) if isinstance(s, dict))
        tag = " · ".join(str(x) for x in (d.get("type"), d.get("significance") and f'{d["significance"]} importance') if x)
        dis.append(f'<div class="topic"><div class="topic-h">{inline(d.get("topic"))}'
                   + (f' <small>{esc(tag)}</small>' if tag else "") + f'</div><div class="sides">{sides}</div></div>')
    block("differ", "Where they differ", "".join(dis))
    block("weak", "Weak or unsupported", _ul(
        [f'{inline(x.get("claim"))} {_who(x, names)}' + (f' <span class="sub">— {inline(x.get("why") or x.get("note") or x.get("reason"))}</span>'
                                                         if (x.get("why") or x.get("note") or x.get("reason")) else "")
         for key in ("factual_conflicts", "unsupported_claims", "likely_errors") for x in res.get(key, []) if isinstance(x, dict)]))
    block("unique", "Only one AI said this", _ul([f'{inline(x.get("insight"))} {_who(x, names)}'
                                                  for x in res.get("unique_insights", []) if isinstance(x, dict)]))
    block("missing", "Not covered or needs checking", _ul(
        [inline(x) for x in res.get("omissions", [])]
        + [inline(x.get("question")) + (f' <span class="sub">— {inline(x["why"])}</span>' if x.get("why") else "")
           for x in res.get("verification_needed", []) if isinstance(x, dict)]))
    ch = res.get("challenge") or {}
    block("challenge", "Strongest challenge to the consensus", _ul(
        [f"<b>Consensus:</b> {inline(ch['current_consensus'])}" if ch.get("current_consensus") else "",
         f"<b>Case against it:</b> {inline(ch['strongest_case_against'])}" if ch.get("strongest_case_against") else "",
         f"<b>What would prove it wrong:</b> {inline(ch['what_would_falsify'])}" if ch.get("what_would_falsify") else "",
         *[f"<b>Untested assumption:</b> {inline(u)}" for u in ch.get("untested_assumptions", [])]]))
    block("next", "What to ask next", _ul([inline(x) for x in res.get("next_challenges", [])]))
    if res.get("best_current_synthesis"):
        block("best", "Best current answer", f'<p>{inline(res["best_current_synthesis"])}</p>')
    if not out:
        return ""
    legend = " · ".join(f"{esc(lbl)} = {esc(n)}" for lbl, (_, n) in sorted(names.items()))
    return (f'<section class="glance"><h3>At a glance <small>compared by {esc(judge)} without knowing who wrote what</small></h3>'
            + name_labels("".join(out), names)
            + (f'<p class="legend">Where a letter is left in the notes: {legend}</p>' if legend else "") + "</section>")


def _fmt_s(ms: int | None) -> str:
    return f"{ms / 1000:.0f}s" if ms else ""


def _answer(app: Any, rnd: Any, run: Any, folder: Path | None, rel: str) -> str:
    pid = run.provider
    name = app.config.providers[pid].display_name if pid in app.config.providers else pid
    color = COLORS.get(pid, "#777")
    meta = run.meta or {}
    bits = [x for x in (run.model_reported or run.model_requested, _fmt_s(run.latency_ms)) if x and x != "default"]
    head = (f'<header><span class="dot"></span><h3>{esc(name)}</h3><span class="meta">{esc(" · ".join(bits))}</span>'
            + ('<button type="button" data-copy="answer">Copy</button>' if run.status == RunStatus.COMPLETE else "") + "</header>")
    sent = ""
    if run.input_text and run.input_text.strip() != (rnd.prompt or "").strip():
        sent = (f'<details class="sent"><summary>What {esc(name)} was sent (your message plus what you chose to share)</summary>'
                f'<pre>{esc(run.input_text.strip())}</pre></details>')
    if run.status == RunStatus.COMPLETE:
        arts = Artifacts(folder, rel, f"round{rnd.seq}-{pid}")
        body = f'<div class="prose">{render_markdown(run.output_text or "", arts)}</div>'
        body += f'<textarea class="raw" hidden>{esc(run.output_text or "")}</textarea>'
    elif run.status == RunStatus.WAITING:
        body = (f'<p class="note">Waiting for you: paste {esc(name)}\'s answer into Cockpit '
                f'(<code>/paste {esc(pid)}</code>) and it will appear here.</p>')
    else:
        body = f'<p class="note bad">{esc(run.status.value)}: {esc(run.error_message or run.error_kind or "no answer")}</p>'
    cites = meta.get("citations") or [s.get("url") for s in meta.get("search_results") or [] if isinstance(s, dict)]
    cites = [c for c in cites if isinstance(c, str) and re.match(r"^https?://", c)]
    foot = ""
    if cites:
        foot += ('<details class="extra" open><summary>Sources</summary><ol>'
                 + "".join(f'<li><a href="{esc(c)}" target="_blank" rel="noopener noreferrer">{esc(c)}</a></li>' for c in cites)
                 + "</ol></details>")
    if meta.get("searches"):
        foot += ('<details class="extra"><summary>Searches it ran</summary><ul>'
                 + "".join(f"<li>{esc(q)}</li>" for q in meta["searches"]) + "</ul></details>")
    return (f'<article class="answer" id="r{rnd.seq}-{esc(pid)}" style="--ai:{color}">{head}{sent}{body}'
            + (f"<footer>{foot}</footer>" if foot else "") + "</article>")


def build_reader(app: Any, ctx: Any, root: Path, name: str) -> Path:
    """Write <root>/<name>.html (plus artifact files in the chat's own folder). Returns the page path."""
    from .cli import write_private

    st = app.store
    detail_rel = f"{quote(ctx.project.name)}/chats/{quote(ctx.chat.name)}/artifacts"
    folder = root / ctx.project.name / "chats" / ctx.chat.name / "artifacts"
    rounds = st.list_rounds(ctx.chat.id)
    branches = {b.id: b.name for b in st.list_branches(ctx.chat.id)}
    disp = lambda p: app.config.providers[p].display_name if p in app.config.providers else p  # noqa: E731
    sections: list[str] = []
    jump: list[str] = []
    for i, rnd in enumerate(rounds):
        last = i == len(rounds) - 1
        runs = [r for r in st.runs_for_round(rnd.id) if r.status != RunStatus.SKIPPED]
        answers = "".join(_answer(app, rnd, r, folder, detail_rel) for r in runs)
        glance = ""
        for a in st.analyses_for_round(rnd.id):
            if a.result:
                names = {str(lbl).upper(): (v["provider"], disp(v["provider"])) for lbl, v in (a.label_map or {}).items()}
                g = glance_html(a, names, disp(a.judge_provider))
                if a.kind == "synthesis":
                    answers += g
                else:
                    glance = g  # the latest comparison wins
        if last:
            jump = [f'<a href="#r{rnd.seq}-{esc(r.provider)}" style="--ai:{COLORS.get(r.provider, "#777")}">{esc(disp(r.provider))}</a>'
                    for r in runs]
            if glance:
                jump.insert(0, f'<a href="#r{rnd.seq}-glance" class="jg">At a glance</a>')
        gist = " ".join((rnd.prompt or "").split())
        branch = branches.get(rnd.branch_id, "")
        sections.append(
            f'<details class="round" id="round-{rnd.seq}"{" open" if last else ""}>'
            f'<summary><span class="rn">Round {rnd.seq}</span><span class="rq">{esc(gist[:110] + ("…" if len(gist) > 110 else ""))}</span>'
            + (f'<span class="rb">branch {esc(branch)}</span>' if branch and branch != "main" else "") + "</summary>"
            f'<div class="you"><div class="you-l">You</div><div class="you-t">{esc(rnd.prompt or "")}</div></div>'
            + (f'<div id="r{rnd.seq}-glance">{glance}</div>' if glance else "")
            + f'<div class="answers">{answers or "<p class=note>No answers in this round.</p>"}</div></details>')
    body = "".join(sections) or '<p class="note">No rounds yet. Ask something in Cockpit and this page will fill in.</p>'
    version = hashlib.sha256(body.encode()).hexdigest()[:16]
    nonce = secrets.token_urlsafe(16)
    vfile = f"{name}.version.js"
    page = _PAGE.format(
        title=esc(ctx.chat.name), project=esc(ctx.project.name), body=body, jump="".join(jump), nonce=nonce,
        version=version, vfile=esc(quote(vfile)), rounds=len(rounds), key=esc(f"{ctx.project.name}/{ctx.chat.name}"),
        csp=esc(f"script-src 'nonce-{nonce}' https://cdnjs.cloudflare.com; object-src 'none'; base-uri 'none'; form-action 'none'"),
        hl_js=HLJS_JS[0], hl_js_i=HLJS_JS[1], hl_l=HLJS_LIGHT[0], hl_l_i=HLJS_LIGHT[1], hl_d=HLJS_DARK[0], hl_d_i=HLJS_DARK[1],
        k_js=KATEX_JS[0], k_js_i=KATEX_JS[1], k_css=KATEX_CSS[0], k_css_i=KATEX_CSS[1], css=_CSS, js=_JS)
    target = root / f"{name}.html"
    write_private(target, page)
    write_private(root / vfile, f'window.__cockpitSaved&&window.__cockpitSaved("{version}");\n')
    return target


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="{csp}">
<meta name="referrer" content="no-referrer">
<title>{title} · LLM Cockpit</title>
<link rel="stylesheet" href="{hl_l}" integrity="{hl_l_i}" crossorigin="anonymous" media="(prefers-color-scheme: light)">
<link rel="stylesheet" href="{hl_d}" integrity="{hl_d_i}" crossorigin="anonymous" media="(prefers-color-scheme: dark)">
<link rel="stylesheet" href="{k_css}" integrity="{k_css_i}" crossorigin="anonymous">
<style>{css}</style></head>
<body data-rounds="{rounds}" data-version="{version}" data-vfile="{vfile}" data-key="{key}">
<div class="top"><div class="top-in">
  <div class="tt"><b>{title}</b><span>{project}</span></div>
  <nav class="jump">{jump}</nav>
  <div class="tools"><button type="button" id="side">Side by side</button><button type="button" id="openall">Open all rounds</button></div>
</div></div>
<main>{body}</main>
<p class="foot">Saved by LLM Cockpit on this computer. This page updates itself after every round.</p>
<script nonce="{nonce}" src="{hl_js}" integrity="{hl_js_i}" crossorigin="anonymous" defer></script>
<script nonce="{nonce}" src="{k_js}" integrity="{k_js_i}" crossorigin="anonymous" defer></script>
<script nonce="{nonce}">{js}</script>
</body></html>
"""

_CSS = """
:root{--bg:#faf9f5;--card:#fff;--fg:#1f1e1d;--muted:#6b6a65;--rule:#e4e2da;--soft:#f1efe8;--you:#ece9df;
--ok:#1f7a4d;--okbg:#e8f4ec;--amber:#8a5a00;--amberbg:#fbf1d9;--red:#a8322d;--redbg:#fae9e7;--blue:#2b5f9e;--bluebg:#e7eff9;
--sans:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;
--serif:ui-serif,"Iowan Old Style","Palatino Linotype",Palatino,Georgia,serif;
--mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace}
@media (prefers-color-scheme:dark){:root{--bg:#1f1e1d;--card:#2a2927;--fg:#ecebe6;--muted:#a3a199;--rule:#3d3b38;--soft:#32302d;--you:#3a3835;
--ok:#7fd3a3;--okbg:#1e3528;--amber:#f0c36a;--amberbg:#3b3017;--red:#f19a94;--redbg:#40211f;--blue:#9cc3f2;--bluebg:#1f2f44}}
*{box-sizing:border-box}
html{scroll-padding-top:calc(var(--top,57px) + 70px)}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 var(--sans);-webkit-text-size-adjust:100%}
button{font:inherit;font-size:13.5px;color:var(--fg);background:var(--card);border:1px solid var(--rule);border-radius:8px;padding:5px 11px;cursor:pointer}
button:hover{background:var(--soft)} button.on{background:var(--fg);color:var(--bg);border-color:var(--fg)}
.top{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--rule)}
.top-in{max-width:1500px;margin:0 auto;padding:10px 20px;display:flex;gap:10px 18px;align-items:center;flex-wrap:wrap}
.tt{display:flex;flex-direction:column;line-height:1.2;min-width:0}.tt b{font-size:17px}.tt span{font-size:12.5px;color:var(--muted)}
.jump{display:flex;gap:6px;flex-wrap:wrap;flex:1}
.jump a{text-decoration:none;color:var(--fg);font-size:14px;padding:4px 11px;border-radius:999px;border:1px solid var(--rule);background:var(--card)}
.jump a::before{content:"";display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--ai,var(--muted));margin-right:7px}
.jump a.jg::before{background:var(--fg)}
.tools{display:flex;gap:6px}
main{max-width:880px;margin:0 auto;padding:22px 20px 40px;display:flex;flex-direction:column;gap:18px}
body.side main{max-width:1500px}
.round>summary{cursor:pointer;list-style:none;display:flex;gap:12px;align-items:baseline;padding:10px 14px;border:1px solid var(--rule);border-radius:10px;background:var(--card)}
.round>summary::-webkit-details-marker{display:none}
.round[open]>summary{border-color:transparent;background:none;padding:6px 2px}
.rn{font-weight:650;white-space:nowrap}.rq{color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}
.round[open] .rq{visibility:hidden}.rb{font-size:12.5px;color:var(--muted);white-space:nowrap}
.you{margin:6px 0 20px auto;max-width:78%;background:var(--you);border-radius:18px;padding:14px 18px}
.you-l{font-size:12px;font-weight:650;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;margin-bottom:3px}
.you-t{white-space:pre-wrap;overflow-wrap:anywhere;font-size:17px;line-height:1.55}
.answers{display:flex;flex-direction:column;gap:22px}
body.side .answers{display:grid;grid-template-columns:repeat(auto-fit,minmax(430px,1fr));align-items:start}
.answer{background:var(--card);border:1px solid var(--rule);border-top:4px solid var(--ai);border-radius:14px;padding:0 26px 18px;min-width:0}
.answer>header{display:flex;align-items:center;gap:10px;margin:0 -26px 16px;padding:13px 26px 11px;border-bottom:1px solid var(--rule);position:sticky;top:var(--top,57px);background:var(--card);z-index:2;border-radius:10px 10px 0 0}
.answer>header h3{margin:0;font-size:18px}.dot{width:11px;height:11px;border-radius:50%;background:var(--ai)}
.answer .meta{color:var(--muted);font-size:13px;flex:1}
.prose{font:18px/1.7 var(--serif);overflow-wrap:anywhere}
.prose>:first-child{margin-top:0}.prose>:last-child{margin-bottom:0}
.prose p{margin:0 0 1em}.prose h1,.prose h2,.prose h3,.prose h4{font-family:var(--sans);line-height:1.3;margin:1.5em 0 .55em}
.prose h1{font-size:1.45em}.prose h2{font-size:1.25em}.prose h3{font-size:1.1em}.prose h4{font-size:1em}
.prose ul,.prose ol{margin:0 0 1em;padding-left:1.5em}.prose li{margin:.3em 0}.prose li>p{margin:.3em 0}
.prose a{color:var(--blue);text-underline-offset:2px}
.prose blockquote{margin:0 0 1em;padding:.1em 1em;border-left:3px solid var(--rule);color:var(--muted)}
.prose hr{border:0;border-top:1px solid var(--rule);margin:1.6em 0}
.prose img{max-width:100%;height:auto;border-radius:10px;margin:.4em 0}
.prose code{font:.85em var(--mono);background:var(--soft);padding:.12em .38em;border-radius:5px}
.noimg{color:var(--muted);font-style:italic}
.tablewrap{overflow-x:auto;margin:0 0 1.1em;border:1px solid var(--rule);border-radius:10px}
.prose table{border-collapse:collapse;width:100%;font:15.5px/1.5 var(--sans);overflow-wrap:normal}
.prose th,.prose td{padding:9px 13px;text-align:left;vertical-align:top;border-bottom:1px solid var(--rule);min-width:6.5em;overflow-wrap:break-word}
.prose th{background:var(--soft);font-weight:650}.prose tr:last-child td{border-bottom:0}
.code{margin:0 0 1.1em;border:1px solid var(--rule);border-radius:10px;overflow:hidden;font-family:var(--sans)}
.codebar{display:flex;justify-content:space-between;align-items:center;padding:5px 8px 5px 14px;background:var(--soft);font-size:12.5px;color:var(--muted)}
.codebar button{padding:2px 9px;font-size:12.5px}
pre{margin:0;padding:14px 16px;overflow-x:auto;font:14px/1.55 var(--mono)}
pre code,.prose pre code{background:none!important;padding:0!important;font:inherit}
.math.display{display:block;text-align:center;margin:1em 0;overflow-x:auto}
.artifact{margin:0 0 1.2em;border:1px solid var(--rule);border-radius:12px;overflow:hidden;background:var(--card);font-family:var(--sans)}
.artifact figcaption{display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:9px 10px 9px 15px;background:var(--soft);border-bottom:1px solid var(--rule)}
.art-title{font-weight:650;font-size:15.5px}.art-kind{font-size:12.5px;color:var(--muted);flex:1}
.art-actions{display:flex;gap:5px}.art-actions button{padding:3px 10px;font-size:12.5px}
.art-preview{resize:vertical;overflow:auto;background:#fff}
.artifact[data-kind=page] .art-preview{height:460px}.artifact[data-kind=diagram] .art-preview{height:300px}
.art-preview iframe{display:block;width:100%;height:100%;border:0;background:#fff}
.art-preview .drawing{display:block;max-width:100%;height:auto;margin:0 auto;padding:14px}
.art-preview .doc{padding:20px 26px;background:var(--card)}
.artifact[data-kind=document] .art-preview{max-height:520px;background:var(--card)}
.art-code pre{max-height:520px}
.artifact.big{position:fixed;inset:14px;z-index:50;margin:0;display:flex;flex-direction:column;box-shadow:0 0 0 100vmax rgba(0,0,0,.55)}
.artifact.big .art-preview,.artifact.big .art-code{flex:1;height:auto!important;max-height:none!important;resize:none}
.artifact.big .art-code pre{max-height:none;height:100%}
.sent{margin:0 0 14px;font-size:14px;color:var(--muted)}.sent summary{cursor:pointer}
.sent pre{white-space:pre-wrap;background:var(--soft);border-radius:8px;margin-top:8px;font-size:13px;max-height:340px}
.answer footer{margin-top:16px;padding-top:12px;border-top:1px solid var(--rule);font-size:14px;color:var(--muted)}
.extra summary{cursor:pointer;font-weight:600}.extra ol,.extra ul{margin:.4em 0 .8em;padding-left:1.4em}.extra a{color:var(--blue);overflow-wrap:anywhere}
.note{color:var(--muted);font-size:16px}.note.bad{color:var(--red)}
.glance{background:var(--card);border:1px solid var(--rule);border-radius:14px;padding:18px 24px 20px;margin-bottom:22px;font-size:16.5px;line-height:1.6}
.glance h3{margin:0 0 8px;font-size:19px}.glance h3 small{font-weight:400;font-size:13px;color:var(--muted);margin-left:8px}
.g-summary{font:17.5px/1.6 var(--serif);margin:0 0 14px}ul.g-summary{padding-left:1.2em}ul.g-summary li{margin:.4em 0}
.legend{margin:12px 0 0;font-size:13px;color:var(--muted)}
.lbl{color:var(--ai);font-family:var(--sans);font-size:.93em;white-space:nowrap}
.g{border-left:4px solid var(--rule);border-radius:0 10px 10px 0;padding:10px 16px;margin-top:10px;background:var(--soft)}
.g h4{margin:0 0 4px;font-size:13px;text-transform:uppercase;letter-spacing:.05em}
.g ul{margin:.2em 0;padding-left:1.25em}.g li{margin:.35em 0}.g p{margin:.2em 0}
.g.agree{border-color:var(--ok);background:var(--okbg)}.g.agree h4{color:var(--ok)}
.g.differ,.g.weak,.g.warn{border-color:var(--amber);background:var(--amberbg)}.g.differ h4,.g.weak h4,.g.warn h4{color:var(--amber)}
.g.challenge{border-color:var(--red);background:var(--redbg)}.g.challenge h4{color:var(--red)}
.g.next,.g.best{border-color:var(--blue);background:var(--bluebg)}.g.next h4,.g.best h4{color:var(--blue)}
.topic{margin:.5em 0 .9em}.topic-h{font-weight:650}.topic-h small{font-weight:400;color:var(--muted);margin-left:6px}
.sides{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:10px;margin-top:7px}
.side{background:var(--card);border:1px solid var(--rule);border-radius:10px;padding:10px 13px}
.pos{font-weight:600}.sub{font-size:14.5px;color:var(--muted);margin-top:3px}
.who{display:inline-flex;gap:4px;flex-wrap:wrap;vertical-align:middle;margin-left:4px}
.chip{font:600 12px var(--sans);padding:1px 8px;border-radius:999px;color:#fff;background:var(--ai);white-space:nowrap}
.foot{text-align:center;color:var(--muted);font-size:13px;padding:0 20px 30px}
@media (max-width:700px){.prose{font-size:17px}.answer{padding:0 16px 14px}.answer>header{margin:0 -16px 14px;padding:12px 16px 10px}.you{max-width:100%}.jump{order:3;flex-basis:100%}}
@media print{.top,.foot,button{display:none}.round:not([open]){display:none}}
"""

_JS = r"""
(function(){
  var B=document.body,V=B.dataset.version,KEY=B.dataset.key,NONCE=document.currentScript.nonce;
  function get(s,k){try{return s.getItem(k)}catch(e){return null}} function set(s,k,v){try{s.setItem(k,v)}catch(e){}}
  var ss=window.sessionStorage,ls=window.localStorage;
  function side(on){B.classList.toggle('side',on);document.getElementById('side').classList.toggle('on',on)}
  side(get(ls,'cockpit-side')==='1');
  function flash(b,t){var o=b.textContent;b.textContent=t;setTimeout(function(){b.textContent=o},1200)}
  function copy(text,b){
    function ok(){flash(b,'Copied')}
    if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(text).then(ok,fallback)}else{fallback()}
    function fallback(){var t=document.createElement('textarea');t.value=text;document.body.appendChild(t);t.select();
      try{document.execCommand('copy');ok()}catch(e){flash(b,'Select and copy')}t.remove()}
  }
  document.addEventListener('click',function(e){
    var b=e.target.closest('button');if(!b)return;
    if(b.id==='side'){var on=!B.classList.contains('side');side(on);set(ls,'cockpit-side',on?'1':'0');return}
    if(b.id==='openall'){var all=document.querySelectorAll('details.round'),open=b.dataset.o!=='1';
      all.forEach(function(d,i){d.open=open||i===all.length-1});b.dataset.o=open?'1':'0';b.textContent=open?'Close older rounds':'Open all rounds';return}
    var fig=b.closest('.artifact');
    if(b.dataset.copy==='answer'){copy(b.closest('.answer').querySelector('textarea.raw').value,b);return}
    if(b.dataset.copy==='code'){copy(b.closest('.code').querySelector('code').textContent,b);return}
    if(b.dataset.copy==='art'&&fig){copy(fig.querySelector('.art-code code').textContent,b);return}
    if(b.dataset.tab&&fig){var code=b.dataset.tab==='code';
      fig.querySelector('.art-preview').hidden=code;fig.querySelector('.art-code').hidden=!code;
      fig.querySelectorAll('[data-tab]').forEach(function(x){x.classList.toggle('on',x===b)});return}
    if(b.hasAttribute('data-expand')&&fig){var big=fig.classList.toggle('big');b.textContent=big?'Close':'Expand';return}
  });
  document.addEventListener('keydown',function(e){if(e.key==='Escape'){document.querySelectorAll('.artifact.big').forEach(function(f){
    f.classList.remove('big');f.querySelector('[data-expand]').textContent='Expand'})}});
  function bar(){document.documentElement.style.setProperty('--top',document.querySelector('.top').offsetHeight+'px')}
  bar();window.addEventListener('resize',bar);
  // A framed artifact reports how tall its content is; only that number is used.
  window.addEventListener('message',function(e){var h=e.data&&e.data.cockpitArtifactHeight;
    if(typeof h!=='number'||!(h>0))return;
    document.querySelectorAll('.artifact iframe').forEach(function(f){
      if(f.contentWindow===e.source){f.parentNode.style.height=Math.max(140,Math.min(760,Math.ceil(h)+2))+'px'}})});
  window.addEventListener('load',function(){
    if(window.hljs){document.querySelectorAll('pre code').forEach(function(el){try{hljs.highlightElement(el)}catch(e){}})}
    if(window.katex){document.querySelectorAll('.math').forEach(function(el){try{
      katex.render(el.dataset.tex,el,{displayMode:el.classList.contains('display'),throwOnError:false})}catch(e){}})}
    var n=+B.dataset.rounds,seen=+(get(ss,'seen:'+KEY)||0),y=get(ss,'y:'+KEY);
    if(n>1&&n!==seen){var r=document.getElementById('round-'+n);if(r)r.scrollIntoView()}
    else if(y&&n===seen){window.scrollTo(0,+y)}
    set(ss,'seen:'+KEY,String(n));
  });
  // The page reloads itself when Cockpit has saved a newer version (after each round).
  window.__cockpitSaved=function(v){if(v!==V){set(ss,'y:'+KEY,String(window.scrollY));location.reload()}};
  function check(){if(document.hidden||document.querySelector('.artifact.big'))return;
    var s=document.createElement('script');s.nonce=NONCE;s.src=B.dataset.vfile+'?t='+Date.now();
    s.onload=s.onerror=function(){s.remove()};document.head.appendChild(s)}
  setInterval(check,3000);document.addEventListener('visibilitychange',check);window.addEventListener('focus',check);
})();
"""
