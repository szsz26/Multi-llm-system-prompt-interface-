"""Reader page: answers formatted like a chat app, artifacts in sandboxed frames, nothing a model wrote runs in the page."""

import argparse
import asyncio
import re
from types import SimpleNamespace

from cockpit.config import DEFAULT_CHAT_STYLE
from cockpit.domain import AnalysisKind
from cockpit.providers.fake import FakeBehavior
from cockpit.reader import Artifacts, build_reader, name_labels, render_markdown

from .conftest import echo_reply

RICH = """# Paying suppliers

| Route | Fee |
|---|---|
| Bank | 3% |

1. First
2. Second

See [the bank](https://www.bcb.gob.bo/) and ![chart](https://example.com/c.png).
It costs $5 and $10. Math: \\(x^2\\) and $$y = mx + b$$

<script>alert("pwned")</script> [bad](javascript:alert(1)) ![local](file:///etc/passwd)

```html
<!doctype html><html><head><title>Fee calculator</title></head>
<body><input id="a"><script>document.title = "ran"</script></body></html>
```

```mermaid
%% title: Money path
flowchart TD
  A[Importer & co] --> B[Bank]
```

```svg
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><title>Logo</title><script>alert(1)</script><circle r="4"/></svg>
```

```html
<a href="x">a short snippet stays code</a>
```
"""


def rich(pid):
    judge = echo_reply(pid)
    return lambda req: judge(req) if "<<cockpit-task:" in req.messages[-1].content else RICH


def test_markdown_is_rendered_and_made_safe():
    out = render_markdown(RICH)
    assert "<table>" in out and "<th>Route</th>" in out and "<ol>" in out and "<h1>Paying suppliers</h1>" in out
    assert '<a href="https://www.bcb.gob.bo/" target="_blank" rel="noopener noreferrer">' in out
    assert '<img src="https://example.com/c.png" alt="chart"' in out
    assert "<script>" not in out and "&lt;script&gt;alert" in out  # shown as text, never run
    tags = "".join(re.findall(r"<[^>]+>", out))
    assert "javascript:" not in tags and "file:" not in tags  # blocked links and local files stay plain text
    assert 'class="math" data-tex="x^2"' in out and 'class="math display" data-tex="y = mx + b"' in out
    assert "$5 and $10" in out  # plain dollar amounts are not math


def test_standalone_pieces_become_sandboxed_artifacts(tmp_path):
    arts = Artifacts(tmp_path / "artifacts", "p/chats/c/artifacts", "round1-claude")
    out = render_markdown(RICH, arts)
    kinds = re.findall(r'class="artifact" data-kind="(\w+)"', out)
    assert kinds == ["page", "diagram", "drawing"]  # the short html snippet stays a code block
    assert "a short snippet stays code" in out and ">Fee calculator<" in out and ">Money path<" in out and ">Logo<" in out
    frames = re.findall(r"<iframe[^>]+>", out)
    assert len(frames) == 2 and all('sandbox="allow-scripts"' in f and "allow-same-origin" not in f for f in frames)
    assert '<img class="drawing" src="p/chats/c/artifacts/round1-claude-4.svg"' in out  # drawings are images: scripts can't run
    assert "document.title = &quot;ran&quot;" in out and 'document.title = "ran"' not in out  # code tab shows it as text
    names = sorted(p.name for p in (tmp_path / "artifacts").iterdir())
    assert names == ["round1-claude-1.html", "round1-claude-2.html", "round1-claude-3.mmd", "round1-claude-4.svg"]
    page = (tmp_path / "artifacts" / "round1-claude-1.html").read_text()
    assert "Content-Security-Policy" in page and "connect-src &#x27;none&#x27;" in page  # the page can't call out
    diagram = (tmp_path / "artifacts" / "round1-claude-2.html").read_text()
    assert "Importer \\u0026 co" in diagram and "mermaid.min.js" in diagram and 'integrity="sha384-' in diagram
    assert all(oct(p.stat().st_mode & 0o777) == "0o600" for p in (tmp_path / "artifacts").iterdir())
    assert "<iframe" not in render_markdown(RICH)  # no folder to write to: everything stays a code block


def test_judge_labels_become_names_but_ordinary_words_do_not():
    names = {"A": ("claude", "Claude"), "B": ("openai", "ChatGPT"), "C": ("grok", "Grok")}
    text = "<li>A recommends X.</li><li>B and C cite Y; A's example. A bank quote. Plan B. Vitamin C. Response A won.</li>"
    out = re.sub(r"<[^>]+>", "", name_labels(text, names))
    assert out == "Claude recommends X.ChatGPT and Grok cite Y; Claude's example. A bank quote. Plan B. Vitamin C. Claude won."


def test_reader_page_for_a_chat(harness, tmp_path):
    h = harness(providers=["claude", "openai", "grok"], behaviors={p: FakeBehavior(reply=rich(p)) for p in ("claude", "openai", "grok")})
    h.send("First question", fresh=True)
    h.send("Second question")
    h.run(h.engine.analyze(h.refresh(), AnalysisKind.ADJUDICATE, judge="claude"))
    app = SimpleNamespace(store=h.store, config=h.engine.config)
    root = tmp_path / "saved"
    page = build_reader(app, h.refresh(), root, "c1")
    html = page.read_text()
    assert page == root / "c1.html" and (root / "c1.version.js").exists()
    assert '<details class="round" id="round-1">' in html and '<details class="round" id="round-2" open>' in html  # older folded
    assert html.count('class="answer"') == 6 and ">ChatGPT</h3>" in html
    assert "At a glance" in html and "Where they agree" in html and "Strongest challenge" in html
    assert re.search(r'<b class="lbl"[^>]*>(Claude|ChatGPT|Grok)</b> was the best', html)  # names, not "Response A"
    assert 'alert("pwned")' not in html and "<script>alert" not in html
    nonce = re.search(r"script-src &#x27;nonce-([\w-]+)&#x27;", html).group(1)
    assert all(f'nonce="{nonce}"' in tag for tag in re.findall(r"<script[^>]*>", html))  # only the page's own scripts run
    assert len(list((root / "p" / "chats" / "c1" / "artifacts").glob("round2-*.html"))) == 6
    # the visible chat-style instruction reaches the AIs, never the judge
    assert h.calls("grok")[-1].system_prompt == DEFAULT_CHAT_STYLE
    assert h.calls("claude")[-1].system_prompt is None and "cockpit-task" in h.calls("claude")[-1].messages[-1].content
    again = build_reader(app, h.refresh(), root, "c1").read_text()
    assert re.search(r'data-version="(\w+)"', again).group(1) == re.search(r'data-version="(\w+)"', html).group(1)


def test_terminal_shows_a_short_recap_when_the_reader_page_is_open(tmp_path, monkeypatch, capsys):
    import cockpit.cli as cli

    opened = []
    monkeypatch.setattr(cli, "open_page", lambda p: opened.append(p) or True)
    app = cli.make_app(argparse.Namespace(home=str(tmp_path / "data"), demo=True, mode="rich"))
    app.interactive = True
    ctx = app.ctx()
    rr = asyncio.run(app.engine.send(ctx, "What is the capital of Bolivia?", fresh=True))
    cli.show_result(app, ctx, rr)
    out = capsys.readouterr().out
    assert "Read the answers in your browser" in out and "answering:" not in out  # no wall of text in the terminal
    assert len(opened) == 1 and opened[0].name == "main.html" and opened[0].read_text().count('class="answer"') >= 3
    assert str(tmp_path / "data") in str(opened[0])  # demo chats are saved with the demo data, not in Documents
    rr = asyncio.run(app.engine.send(app.ctx(), "And the seat of government?", fresh=False))
    cli.show_result(app, app.ctx(), rr)
    assert len(opened) == 1  # the browser is opened once; the page updates itself after that
    app.reader_on = False
    rr = asyncio.run(app.engine.send(app.ctx(), "Third", fresh=False))
    capsys.readouterr()
    cli.show_result(app, app.ctx(), rr)
    assert "answering:" in capsys.readouterr().out  # /reader off: full answers back in the terminal
