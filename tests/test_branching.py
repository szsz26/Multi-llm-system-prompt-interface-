"""§61 Branch integrity and Branch context."""

import pytest

from cockpit.domain import CockpitError, RunStatus
from cockpit.engine import Selection

from .conftest import secret


def build(h):
    h.send("R1: analyze X", fresh=True)
    h.send("R2: focus on regulation")
    h.send("R3: and pricing")
    h.send("R4: final view")


def test_edit_creates_branch_and_preserves_old(harness):
    h = harness(providers=["claude", "gemini"])
    build(h)
    main = h.refresh()
    new_ctx, res = h.run(h.engine.edit(main, 2, "R2': focus on FX controls", sink=h.sink))
    assert new_ctx.branch.id != main.branch.id and new_ctx.branch.parent_branch_id == main.branch.id
    assert h.store.get_chat_by_id(main.chat.id).current_branch_id == new_ctx.branch.id
    # old branch fully intact
    assert [r.prompt[:3] for r in h.store.lineage(main.branch.id)] == ["R1:", "R2:", "R3:", "R4:"]
    # new branch = R1 + edited R2'
    assert [r.prompt[:3] for r in h.store.lineage(new_ctx.branch.id)] == ["R1:", "R2'"]
    assert res.round.edited_from_round_id is not None and res.round.seq == 5


def test_branch_context_excludes_later_rounds_of_old_branch(harness):
    h = harness(providers=["claude", "gemini"])
    build(h)
    new_ctx, _ = h.run(h.engine.edit(h.refresh(), 3, "R3': pricing in USD", sink=h.sink))
    msgs = [m.content for m in h.calls("gemini")[-1].messages]
    assert msgs[0].startswith("R1") and msgs[2].startswith("R2") and msgs[4].startswith("R3'")
    assert len(msgs) == 5
    assert not any(m.startswith(("R3:", "R4:")) for m in msgs)
    # follow-ups on the new branch keep building on it, not on R4
    h.ctx = new_ctx
    h.send("R6 on branch")
    msgs = [m.content for m in h.calls("gemini")[-1].messages]
    assert not any(m.startswith("R4:") for m in msgs) and msgs[-1] == "R6 on branch"


def test_switching_back_to_old_branch_continues_old_context(harness):
    h = harness(providers=["claude", "gemini"])
    build(h)
    main = h.refresh()
    h.run(h.engine.edit(main, 2, "R2': other path", sink=h.sink))
    back = h.engine.switch_branch(h.refresh(), "main")
    h.ctx = back
    h.send("R6 on main")
    msgs = [m.content for m in h.calls("claude")[-1].messages]
    assert msgs[6].startswith("R4:") and msgs[-1] == "R6 on main"
    assert not any(m.startswith("R2'") for m in msgs)


def test_edit_selected_lanes_and_fresh_context(harness):
    h = harness(providers=["claude", "gemini"])
    build(h)
    _, res = h.run(h.engine.edit(h.refresh(), 2, "R2'' fresh", selection=Selection(only=["claude"]),
                                 context="fresh", sink=h.sink))
    assert {r.provider: r.status for r in res.runs} == {"claude": RunStatus.COMPLETE, "gemini": RunStatus.SKIPPED}
    assert [m.content for m in h.calls("claude")[-1].messages] == ["R2'' fresh"]


def test_lane_skipped_before_fork_reconstructs_correctly(harness):
    h = harness(providers=["claude", "gemini"])
    h.send("R1", fresh=True)
    h.send("R2 claude only", selection=Selection(only=["claude"]))
    h.send("R3 both")
    h.run(h.engine.edit(h.refresh(), 3, "R3' both", sink=h.sink))
    g = [m.content for m in h.calls("gemini")[-1].messages]
    assert g[0] == "R1" and g[1].startswith(secret("gemini")) and g[2] == "R3' both"


def test_cannot_edit_round_from_another_branch(harness):
    h = harness(providers=["claude", "gemini"])
    build(h)
    nb, _ = h.run(h.engine.edit(h.refresh(), 2, "R2'", sink=h.sink))
    with pytest.raises(CockpitError, match="not on the current branch"):
        h.run(h.engine.edit(nb, 4, "R4'"))


def test_native_session_not_reused_on_new_branch(harness):
    h = harness(providers=["claude", "gemini"])
    build(h)
    main = h.refresh()
    h.store.set_head(main.branch.id, "claude", h.store.get_head(main.branch.id, "claude").head_message_id,
                     "native-123", h.store.get_head(main.branch.id, "claude").head_message_id)
    h.run(h.engine.edit(main, 3, "R3'", sink=h.sink))
    assert h.calls("claude")[-1].native_session_id is None  # canonical transcript replayed instead
