"""Regression tests for the triage auto-recovery patch (2026-08-10).

Incident: a card assigned to a gate-required role got no review-gate card
within `gate_grace_seconds`, was auto-blocked (kind=needs_input), a human
unblocked it before the gate existed, and it was immediately re-blocked for
the same reason — tripping BLOCK_RECURRENCE_LIMIT and landing in `triage`.
`triage` is a deliberate human-in-the-loop parking state
(`block_task`'s BLOCK_RECURRENCE_LIMIT branch) with no automatic path back
out. On the real `compliance` board this stranded 8 cards for 2 days: the
review-gate card was created 43 minutes later, but nothing ever promoted the
triage'd root card back to `todo` once it existed.

`_recover_gate_stalled_triage()` closes that gap: each dispatch tick, any
`triage` card whose most recent `block_loop_detected` reason names "no
review gate" is promoted back to `todo` (then folded into `ready` the same
tick via a follow-up `recompute_ready()`) once `has_review_gate()` is true.
Narrowly scoped to that one cause — a card in `triage` for any other reason
(ADR-19 object-fidelity, a worker's own needs_input) must be left alone.
"""
from __future__ import annotations

import sys
import tempfile

import pytest


@pytest.fixture()
def isolated_kanban_home(monkeypatch):
    test_home = tempfile.mkdtemp(prefix="kanban_triage_recovery_test_")
    monkeypatch.setenv("HERMES_HOME", test_home)
    for mod in list(sys.modules.keys()):
        if mod.startswith("hermes_cli") or mod.startswith("hermes_state") or mod == "hermes_constants":
            del sys.modules[mod]
    from hermes_cli import kanban_db
    yield kanban_db, test_home


def _fake_spawn(*args, **kwargs):
    return 12345


GATE_ASSIGNEES = frozenset({"default"})


def _drive_into_triage(kb, conn, task_id):
    """Replay the real incident's sequence: block for no-gate, get manually
    unblocked while the gate still doesn't exist, re-block for the same
    cause -> BLOCK_RECURRENCE_LIMIT trips -> triage."""
    res1 = kb.dispatch_once(
        conn, spawn_fn=_fake_spawn, dry_run=False,
        gate_required_assignees=GATE_ASSIGNEES, gate_grace_seconds=0,
    )
    assert task_id in res1.auto_blocked
    row = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
    assert row["status"] == "blocked"

    assert kb.unblock_task(conn, task_id)

    res2 = kb.dispatch_once(
        conn, spawn_fn=_fake_spawn, dry_run=False,
        gate_required_assignees=GATE_ASSIGNEES, gate_grace_seconds=0,
    )
    row = conn.execute(
        "SELECT status, block_kind FROM tasks WHERE id=?", (task_id,)
    ).fetchone()
    assert row["status"] == "triage", res2
    assert row["block_kind"] == "needs_input"


def test_triage_card_recovers_once_gate_appears(isolated_kanban_home):
    kb, _home = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = kb.create_task(conn, title="ocr chain root", assignee="default")
    with kb.connect_closing() as conn:
        _drive_into_triage(kb, conn, task_id)

    # Gate still missing: an ordinary tick must NOT touch the triage'd card.
    with kb.connect_closing() as conn:
        res_still_stuck = kb.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=False,
            gate_required_assignees=GATE_ASSIGNEES, gate_grace_seconds=0,
        )
    assert res_still_stuck.recovered_from_triage == []
    with kb.connect_closing() as conn:
        row = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
        assert row["status"] == "triage"

    # Gate created (mirrors a human/Conductor creating the missing critic
    # card and linking it) -> the next tick must recover and dispatch it.
    with kb.connect_closing() as conn:
        kb.create_task(conn, title="gate", assignee="critic", parents=[task_id])
    with kb.connect_closing() as conn:
        res_recovered = kb.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=False,
            gate_required_assignees=GATE_ASSIGNEES, gate_grace_seconds=0,
        )
    assert res_recovered.recovered_from_triage == [task_id]
    assert any(s[0] == task_id for s in res_recovered.spawned), res_recovered
    with kb.connect_closing() as conn:
        row = conn.execute(
            "SELECT status, block_kind, block_recurrences FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        assert row["status"] == "running"
        assert row["block_kind"] is None
        assert row["block_recurrences"] == 0


def test_triage_card_for_an_unrelated_reason_is_left_alone(isolated_kanban_home):
    """A card in triage for any OTHER cause must never be silently promoted,
    even once something that looks like a review gate exists — recovery
    must be scoped to the exact "no review gate" reason, not to "any
    triage card with a gate-assignee child somewhere in the graph"."""
    kb, _home = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = kb.create_task(conn, title="paywalled source", assignee="default")
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (task_id,))
        assert kb.block_task(
            conn, task_id, reason="hit a paywall, need credentials", kind="needs_input",
        )
        assert kb.unblock_task(conn, task_id)
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (task_id,))
        assert kb.block_task(
            conn, task_id, reason="hit a paywall, need credentials", kind="needs_input",
        )
        row = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
        assert row["status"] == "triage"
        # Even with an unrelated gate-assignee card now pointing at it...
        kb.create_task(conn, title="gate", assignee="critic", parents=[task_id])

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=False,
            gate_required_assignees=GATE_ASSIGNEES, gate_grace_seconds=0,
        )
    assert res.recovered_from_triage == []
    with kb.connect_closing() as conn:
        row = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
        assert row["status"] == "triage"
