"""Regression tests for the review-gate guard (kanban.gate_required_assignees).

Closes a create-then-link race. An orchestrator that creates the worker card
first and links its review gate a moment later leaves a window in which the
dispatcher claims and runs the card with nothing gating it — by the time the
gate appears the work is done and already delivered. Prompt-level ordering
rules cannot close this: the orchestrator's own account of what it did is not
evidence of the order it did it in. Only the dispatcher can refuse to start.

A ready card owned by an assignee listed in ``kanban.gate_required_assignees``
is deferred (``skipped_ungated``) while no card assigned to
``kanban.gate_assignee`` (default "critic") depends on it, then auto-blocked
(``needs_input``) after ``kanban.gate_grace_seconds`` if the gate never
arrives. Empty ``gate_required_assignees`` (the default) means the guard is
off.

Tests use assignee="default" for dispatchable cards: ``profile_exists``
always returns True for "default" without needing an on-disk profiles/
directory. ``gate_required_assignees`` is passed explicitly per test — it
does not need to match a real roles.yaml population, just the guard's own
scoping.
"""
from __future__ import annotations

import sys
import tempfile
import time

import pytest


@pytest.fixture()
def isolated_kanban_home(monkeypatch):
    """Spin up a fresh HERMES_HOME with a clean kanban DB."""
    test_home = tempfile.mkdtemp(prefix="kanban_review_gate_test_")
    monkeypatch.setenv("HERMES_HOME", test_home)
    for mod in list(sys.modules.keys()):
        if mod.startswith("hermes_cli") or mod.startswith("hermes_state") or mod == "hermes_constants":
            del sys.modules[mod]
    from hermes_cli import kanban_db
    yield kanban_db, test_home


def _fake_spawn(*args, **kwargs):
    return 12345


def _add_review_gate(kb, conn, work_id, gate_assignee="critic"):
    """Create a gate card (assigned to ``gate_assignee``) blocked-by ``work_id``,
    satisfying ``has_review_gate()``. Returns the gate card id."""
    return kb.create_task(
        conn, title="review gate", assignee=gate_assignee, parents=[work_id],
    )


# --- has_review_gate() — DB primitive ---

def test_has_review_gate_true_when_gate_child_depends(isolated_kanban_home):
    kb, _home = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        work_id = kb.create_task(conn, title="work", assignee="default")
        _add_review_gate(kb, conn, work_id)
    with kb.connect_closing() as conn:
        assert kb.has_review_gate(conn, work_id) is True


def test_has_review_gate_false_when_no_gate(isolated_kanban_home):
    kb, _home = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        work_id = kb.create_task(conn, title="work", assignee="default")
    with kb.connect_closing() as conn:
        assert kb.has_review_gate(conn, work_id) is False


def test_has_review_gate_false_when_gate_archived(isolated_kanban_home):
    kb, _home = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        work_id = kb.create_task(conn, title="work", assignee="default")
        gate_id = _add_review_gate(kb, conn, work_id)
        conn.execute("UPDATE tasks SET status='archived' WHERE id=?", (gate_id,))
    with kb.connect_closing() as conn:
        assert kb.has_review_gate(conn, work_id) is False


def test_has_review_gate_false_when_gate_assignee_differs(isolated_kanban_home):
    """A gate card assigned to anyone other than gate_assignee does not count."""
    kb, _home = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        work_id = kb.create_task(conn, title="work", assignee="default")
        _add_review_gate(kb, conn, work_id, gate_assignee="reviewer")
    with kb.connect_closing() as conn:
        assert kb.has_review_gate(conn, work_id, gate_assignee="critic") is False


def test_has_review_gate_false_for_empty_args(isolated_kanban_home):
    kb, _home = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        work_id = kb.create_task(conn, title="work", assignee="default")
        _add_review_gate(kb, conn, work_id)
    with kb.connect_closing() as conn:
        assert kb.has_review_gate(conn, "") is False
        assert kb.has_review_gate(conn, work_id, "") is False


# --- dispatch-level deferral / blocking ---

def test_ungated_card_deferred_not_run(isolated_kanban_home):
    kb, _home = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        work_id = kb.create_task(conn, title="work", assignee="default")
        # gate not linked yet — the create-then-link window
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=False,
            gate_required_assignees=frozenset({"default"}),
        )
    assert res.skipped_ungated == [work_id]
    assert not res.spawned


def test_card_runs_once_gate_linked(isolated_kanban_home):
    kb, _home = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        work_id = kb.create_task(conn, title="work", assignee="default")
        _add_review_gate(kb, conn, work_id)
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=False,
            gate_required_assignees=frozenset({"default"}),
        )
    assert res.skipped_ungated == []
    assert any(s[0] == work_id for s in res.spawned)


def test_non_listed_assignee_unaffected(isolated_kanban_home):
    """A card whose assignee is not in gate_required_assignees runs even without
    a gate — the guard is scoped to the gated population, not every card."""
    kb, _home = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        work_id = kb.create_task(conn, title="work", assignee="default")
        # no gate
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=False,
            gate_required_assignees=frozenset({"researcher"}),
        )
    assert res.skipped_ungated == []
    assert any(s[0] == work_id for s in res.spawned)


def test_guard_off_by_default(isolated_kanban_home):
    """None gate_required_assignees (the default) = feature off; card runs."""
    kb, _home = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        work_id = kb.create_task(conn, title="work", assignee="default")
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=False,
            gate_required_assignees=None,
        )
    assert res.skipped_ungated == []
    assert any(s[0] == work_id for s in res.spawned)


def test_past_grace_card_auto_blocked(isolated_kanban_home):
    """A card still ungated after gate_grace_seconds is auto-blocked (needs_input)
    instead of running ungated or parking forever."""
    kb, _home = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        work_id = kb.create_task(conn, title="work", assignee="default")
        conn.execute(
            "UPDATE tasks SET created_at=? WHERE id=?",
            (int(time.time()) - 1000, work_id),
        )
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=False,
            gate_required_assignees=frozenset({"default"}),
            gate_grace_seconds=300,
        )
    assert res.auto_blocked == [work_id]
    assert res.skipped_ungated == []
    assert not res.spawned
    with kb.connect_closing() as conn:
        row = conn.execute("SELECT status FROM tasks WHERE id=?", (work_id,)).fetchone()
    assert row["status"] == "blocked"


def test_blocked_card_stays_blocked(isolated_kanban_home):
    """Once auto-blocked, the card must not re-enter ready and re-block on the
    next tick (the gate is still not coming)."""
    kb, _home = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        work_id = kb.create_task(conn, title="work", assignee="default")
        conn.execute(
            "UPDATE tasks SET created_at=? WHERE id=?",
            (int(time.time()) - 1000, work_id),
        )
    with kb.connect_closing() as conn:
        res1 = kb.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=False,
            gate_required_assignees=frozenset({"default"}),
            gate_grace_seconds=300,
        )
    assert res1.auto_blocked == [work_id]
    with kb.connect_closing() as conn:
        res2 = kb.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=False,
            gate_required_assignees=frozenset({"default"}),
            gate_grace_seconds=300,
        )
    assert res2.auto_blocked == []
    assert res2.skipped_ungated == []
    assert not res2.spawned
    with kb.connect_closing() as conn:
        row = conn.execute("SELECT status FROM tasks WHERE id=?", (work_id,)).fetchone()
    assert row["status"] == "blocked"
