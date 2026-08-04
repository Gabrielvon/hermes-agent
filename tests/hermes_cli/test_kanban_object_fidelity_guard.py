"""Regression tests for ADR-19 — the object-fidelity dispatch guard.

2026-08-03 incident: Conductor decomposed a root card naming「半鞅」into a
child card naming「半鞍」(a one-character near-miss), and every downstream
fact-check gate PASSed because the swapped-in entity was itself real. ADR-6
verifies "is this information true", never "is this the same object the root
card named". This guard closes that gap at dispatch time: a producer card
whose quoted entities look like a near-miss transcription of a root-card
entity is blocked (kind=needs_input) instead of dispatched. See
DECISIONS.md D-63 for the full design discussion.

Tests use assignee="default" for dispatchable cards: ``profile_exists``
always returns True for "default" without needing an on-disk profiles/
directory, keeping these tests independent of any real profile fixture.
``gate_required_assignees`` is passed explicitly per test — it does not need
to match a real roles.yaml population, just the guard's own scoping.
"""
from __future__ import annotations

import sys
import tempfile

import pytest


@pytest.fixture()
def isolated_kanban_home(monkeypatch):
    """Spin up a fresh HERMES_HOME with a clean kanban DB."""
    test_home = tempfile.mkdtemp(prefix="kanban_object_fidelity_test_")
    monkeypatch.setenv("HERMES_HOME", test_home)
    for mod in list(sys.modules.keys()):
        if mod.startswith("hermes_cli") or mod.startswith("hermes_state") or mod == "hermes_constants":
            del sys.modules[mod]
    from hermes_cli import kanban_db
    yield kanban_db, test_home


def _fake_spawn(*args, **kwargs):
    return 12345


ROOT_BODY = "「半鞅」这家管理人的股票类产品最近表现怎么样？"


def _make_root_and_child(kb, conn, child_body, *, child_assignee="default"):
    root_id = kb.create_task(conn, title="root", body=ROOT_BODY, assignee="conductor")
    conn.execute("UPDATE tasks SET status='done' WHERE id=?", (root_id,))
    child_id = kb.create_task(
        conn, title="child", body=child_body, assignee=child_assignee, parents=[root_id],
    )
    return root_id, child_id


def _add_review_gate(kb, conn, child_id):
    """Satisfy has_review_gate() so ADR-6's separate review-gate check (which
    the object-fidelity guard runs alongside, not instead of) doesn't defer
    the card for an unrelated reason in tests that expect a clean dispatch."""
    kb.create_task(conn, title="gate", assignee="critic", parents=[child_id])


def test_exact_match_dispatches_normally(isolated_kanban_home):
    kb, _home = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        _root_id, child_id = _make_root_and_child(
            kb, conn, "去查一下「半鞅」这家私募的产品线",
        )
        _add_review_gate(kb, conn, child_id)
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=False,
            gate_required_assignees=frozenset({"default"}),
        )
    assert res.auto_blocked_fidelity == []
    assert any(s[0] == child_id for s in res.spawned)


def test_near_miss_transcription_blocks_with_needs_input(isolated_kanban_home):
    kb, _home = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        _root_id, child_id = _make_root_and_child(
            kb, conn, "去查一下「半鞍」这家私募的产品线",
        )
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=False,
            gate_required_assignees=frozenset({"default"}),
        )
    assert res.auto_blocked_fidelity == [child_id]
    assert not res.spawned
    with kb.connect_closing() as conn:
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (child_id,),
        ).fetchone()
    assert row["status"] == "blocked"


def test_wholly_new_entity_not_blocked(isolated_kanban_home):
    """A child card that never mentions the root's entity at all — e.g. it's
    the first of several parallel research cards on a different angle —
    must not be blocked just because it doesn't repeat the root's quotes."""
    kb, _home = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        _root_id, child_id = _make_root_and_child(
            kb, conn, "查一下「联想集团」最近的财报",
        )
        _add_review_gate(kb, conn, child_id)
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=False,
            gate_required_assignees=frozenset({"default"}),
        )
    assert res.auto_blocked_fidelity == []
    assert any(s[0] == child_id for s in res.spawned)


def test_guard_only_applies_to_gated_assignees(isolated_kanban_home):
    """A near-miss on a card whose assignee isn't in gate_required_assignees
    must not be blocked — the guard is scoped to the same producer
    population ADR-6's review gate already covers, not every card."""
    kb, _home = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        _root_id, child_id = _make_root_and_child(
            kb, conn, "综合一下「半鞍」的分析结果",
        )
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=False,
            # "default" (the child's assignee) deliberately NOT included.
            gate_required_assignees=frozenset({"researcher"}),
        )
    assert res.auto_blocked_fidelity == []
    assert any(s[0] == child_id for s in res.spawned)


def test_unblock_alone_does_not_bypass_the_guard(isolated_kanban_home):
    """A bare unblock must NOT be a free pass: the guard is content-based, so
    if nobody actually fixed the card's text, the very next tick blocks it
    again. This is deliberate (mirrors block_task's block_recurrences design,
    which never resets on unblock) — a heuristic this blunt should not be
    escapable by reflexively clicking 'unblock' without addressing the
    underlying mismatch. Real remediation is to fix the card's wording (or,
    after enough repeats, block_task's own recurrence limit escalates the
    task to 'triage' instead of looping in 'blocked' forever)."""
    kb, _home = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        _root_id, child_id = _make_root_and_child(
            kb, conn, "去查一下「半鞍」这家私募的产品线",
        )
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=False,
            gate_required_assignees=frozenset({"default"}),
        )
    assert res.auto_blocked_fidelity == [child_id]

    with kb.connect_closing() as conn:
        assert kb.unblock_task(conn, child_id)

    with kb.connect_closing() as conn:
        res2 = kb.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=False,
            gate_required_assignees=frozenset({"default"}),
        )
    assert res2.auto_blocked_fidelity == [child_id]
    assert not res2.spawned


def test_fixing_the_wording_then_unblocking_lets_it_dispatch(isolated_kanban_home):
    """The real remediation path: a human edits the card so the mismatch is
    actually gone (here: the near-miss quote is corrected to match the root),
    then unblocks it. The next tick dispatches normally."""
    kb, _home = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        _root_id, child_id = _make_root_and_child(
            kb, conn, "去查一下「半鞍」这家私募的产品线",
        )
        _add_review_gate(kb, conn, child_id)
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=False,
            gate_required_assignees=frozenset({"default"}),
        )
    assert res.auto_blocked_fidelity == [child_id]

    with kb.connect_closing() as conn:
        conn.execute(
            "UPDATE tasks SET body = ? WHERE id = ?",
            ("去查一下「半鞅」这家私募的产品线", child_id),
        )
        assert kb.unblock_task(conn, child_id)

    with kb.connect_closing() as conn:
        res2 = kb.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=False,
            gate_required_assignees=frozenset({"default"}),
        )
    assert res2.auto_blocked_fidelity == []
    assert any(s[0] == child_id for s in res2.spawned)
