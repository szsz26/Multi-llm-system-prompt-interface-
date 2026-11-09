"""§61 Isolation, Project isolation, Chat isolation."""

from cockpit.domain import RunStatus

from .conftest import PROVIDERS, secret


def test_lanes_never_see_each_others_responses(harness):
    h = harness()
    h.send("Analyze Company X.", fresh=True)
    h.send("Challenge your assumptions.")
    h.send("Would you change your recommendation?")
    for pid in PROVIDERS:
        req = h.calls(pid)[-1]
        ctx = "\n".join(m.content for m in req.messages)
        assert ctx.count(secret(pid)) == 2
        assert [m.role for m in req.messages] == ["user", "assistant", "user", "assistant", "user"]
        for other in PROVIDERS:
            if other != pid:
                assert secret(other) not in ctx, f"{other} leaked into {pid}"


def test_same_prompt_verbatim_to_every_lane(harness):
    h = harness()
    h.send("Exact wording here.", fresh=True)
    assert {h.calls(p)[-1].messages[-1].content for p in PROVIDERS} == {"Exact wording here."}
    assert all(h.calls(p)[-1].system_prompt is None for p in PROVIDERS)  # no hidden global prompt


def test_chat_isolation(harness):
    h = harness(providers=["claude", "openai"])
    a, b = h.chat("bolivia"), h.chat("identity")
    h.send("bolivia prompt", ctx=a, fresh=True)
    h.send("identity prompt", ctx=b, fresh=True)
    h.send("bolivia follow-up", ctx=a)
    h.send("identity follow-up", ctx=b)
    assert "identity" not in h.context_of("claude", 2)
    assert "bolivia" not in h.context_of("claude", 3)
    assert [r.seq for r in h.store.list_rounds(a.chat.id)] == [1, 2]
    assert [r.seq for r in h.store.list_rounds(b.chat.id)] == [1, 2]


def test_project_isolation(harness):
    h = harness(providers=["claude", "openai"])
    iris, kraken = h.chat("main", project="iris"), h.chat("main", project="kraken")
    h.send("iris secret plan", ctx=iris, fresh=True)
    h.send("kraken question", ctx=kraken)
    assert "iris" not in h.context_of("claude")
    assert iris.chat.id != kraken.chat.id
    h.engine.set_enabled(iris, ["openai"], False)
    assert h.engine.is_enabled(kraken.chat, "openai")  # mute state is per chat


def test_fresh_chat_means_fresh_lanes(harness):
    h = harness(providers=["claude", "openai"])
    h.send("old topic", fresh=True)
    new = h.chat("brand-new")
    h.send("new topic", ctx=new)  # follow-up in a new chat: no context carried over
    assert [m.content for m in h.calls("claude")[-1].messages] == ["new topic"]


def test_blast_resets_lane_context_but_keeps_history(harness):
    h = harness(providers=["claude", "openai"])
    h.send("topic one", fresh=True)
    h.send("more on one")
    h.send("brand new topic", fresh=True)
    assert [m.content for m in h.calls("claude")[-1].messages] == ["brand new topic"]
    total = h.store.db.execute("SELECT COUNT(*) FROM messages WHERE provider='claude'").fetchone()[0]
    assert total == 6  # nothing deleted


def test_followup_without_context_starts_lane(harness):
    h = harness(providers=["claude", "openai"])
    res = h.send("first ever prompt")
    assert all(r.status == RunStatus.COMPLETE and r.meta.get("new_thread") for r in res.runs)
