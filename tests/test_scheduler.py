"""Unit tests for the postmortem action-item scheduler.

Run with: python3 -m unittest discover -s tests -v
All owners use fictional role tokens; no real personnel data.
"""

import unittest
from datetime import date, timedelta

from postmortem_scheduler import (
    ActionItem,
    FreezeWindow,
    Owner,
    Scheduler,
    VERIFY_AUTO,
    VERIFY_MANUAL,
)
from postmortem_scheduler import planner

START = date(2026, 11, 23)  # Monday


def make_item(item_id, owner="owner-alpha", effort=1.0, deadline=None, **kw):
    return ActionItem(
        item_id=item_id,
        title="title-%s" % item_id,
        owner=owner,
        effort_days=effort,
        deadline=deadline or (START + timedelta(days=30)),
        **kw
    )


def plan(items, owners=None, freezes=None):
    owners = owners or [Owner("owner-alpha", 40), Owner("owner-beta", 40)]
    return Scheduler(items, owners, freezes, START).plan()


def scheduled(result, item_id):
    for entry in result.order:
        if entry.item_id == item_id:
            return entry
    raise AssertionError("item %s not scheduled" % item_id)


def risk_kinds(result, item_id):
    return {r.kind for r in result.risks if r.item_id == item_id}


def wait_reasons(result, item_id):
    return {w.reason for w in result.waiting if w.item_id == item_id}


class ExecutionOrderTests(unittest.TestCase):
    def test_dependencies_run_before_dependents(self):
        items = [
            make_item("fix-1", effort=2),
            make_item("verify-1", kind="verify", verification=VERIFY_AUTO,
                      depends_on=["fix-1"]),
        ]
        result = plan(items)
        fix = scheduled(result, "fix-1")
        verify = scheduled(result, "verify-1")
        self.assertEqual(fix.start, START)
        self.assertGreater(verify.start, fix.end)

    def test_same_owner_runs_sequentially(self):
        items = [
            make_item("a", effort=2),
            make_item("b", effort=2),
        ]
        result = plan(items)
        first = scheduled(result, "a")
        second = scheduled(result, "b")
        self.assertGreater(second.start, first.end)

    def test_critical_path_follows_longest_chain(self):
        items = [
            make_item("fix-a", effort=2),
            make_item("fix-b", effort=3, depends_on=["fix-a"]),
            make_item("fix-c", owner="owner-beta", effort=1),
        ]
        result = plan(items)
        self.assertEqual(result.critical_path, ["fix-a", "fix-b"])

    def test_deadline_missed_is_flagged(self):
        items = [make_item("late", effort=10,
                           deadline=START + timedelta(days=2))]
        result = plan(items)
        self.assertIn(planner.RISK_DEADLINE, risk_kinds(result, "late"))


class DependencyCycleTests(unittest.TestCase):
    def test_cycle_is_detected_and_blocked(self):
        items = [
            make_item("a", depends_on=["c"]),
            make_item("b", depends_on=["a"]),
            make_item("c", depends_on=["b"]),
            make_item("d"),
        ]
        result = plan(items)
        self.assertEqual(result.cycles, [["a", "b", "c"]])
        for iid in ("a", "b", "c"):
            self.assertIn(planner.RISK_CYCLE, risk_kinds(result, iid))
            self.assertIn(planner.WAIT_CYCLE, wait_reasons(result, iid))
        # Item outside the cycle is still scheduled.
        self.assertEqual(scheduled(result, "d").status, planner.STATUS_SCHEDULED)
        self.assertEqual(result.critical_path, ["d"])

    def test_self_loop_is_a_cycle(self):
        result = plan([make_item("self", depends_on=["self"])])
        self.assertEqual(result.cycles, [["self"]])


class EvidenceTests(unittest.TestCase):
    def test_manual_item_without_evidence_cannot_close(self):
        items = [make_item("fix-1", verification=VERIFY_MANUAL)]
        result = plan(items)
        self.assertIn(planner.RISK_EVIDENCE, risk_kinds(result, "fix-1"))
        self.assertIn(planner.WAIT_APPROVAL, wait_reasons(result, "fix-1"))

    def test_manual_item_with_evidence_is_clean(self):
        items = [make_item("fix-1", verification=VERIFY_MANUAL,
                           verification_evidence=["report://example/1"])]
        result = plan(items)
        self.assertNotIn(planner.RISK_EVIDENCE, risk_kinds(result, "fix-1"))

    def test_auto_verified_item_needs_no_manual_evidence(self):
        items = [make_item("verify-1", kind="verify",
                           verification=VERIFY_AUTO)]
        result = plan(items)
        self.assertNotIn(planner.RISK_EVIDENCE, risk_kinds(result, "verify-1"))
        self.assertEqual(wait_reasons(result, "verify-1"), set())


class OwnerCapacityTests(unittest.TestCase):
    def test_overloaded_owner_defers_item(self):
        owners = [Owner("owner-alpha", 3)]
        items = [
            make_item("first", effort=2,
                      deadline=START + timedelta(days=5)),
            make_item("second", effort=2,
                      deadline=START + timedelta(days=9)),
        ]
        result = plan(items, owners=owners)
        self.assertEqual(scheduled(result, "first").status,
                         planner.STATUS_SCHEDULED)
        self.assertEqual(scheduled(result, "second").status,
                         planner.STATUS_DEFERRED)
        self.assertIn(planner.RISK_CAPACITY, risk_kinds(result, "second"))
        self.assertIn(planner.WAIT_CAPACITY, wait_reasons(result, "second"))

    def test_splittable_item_is_partially_scheduled(self):
        owners = [Owner("owner-alpha", 3)]
        items = [make_item("big", effort=5, splittable=True)]
        result = plan(items, owners=owners)
        entry = scheduled(result, "big")
        self.assertEqual(entry.status, planner.STATUS_SPLIT)
        self.assertIn("3.0 of 5.0", entry.note)
        self.assertIn(planner.RISK_CAPACITY, risk_kinds(result, "big"))
        self.assertIn(planner.WAIT_CAPACITY, wait_reasons(result, "big"))


class FreezeWindowTests(unittest.TestCase):
    def test_work_skips_freeze_window(self):
        freeze = FreezeWindow(START + timedelta(days=1),
                              START + timedelta(days=3), "release freeze")
        items = [make_item("fix-1", effort=4)]
        result = plan(items, freezes=[freeze])
        entry = scheduled(result, "fix-1")
        self.assertEqual(entry.start, START)
        # 1 day before the freeze + 3 days after it.
        self.assertEqual(entry.end, START + timedelta(days=6))
        # No scheduled working day may fall inside the freeze window.
        worked = [START] + [START + timedelta(days=d) for d in (4, 5, 6)]
        for worked_day in worked:
            self.assertFalse(freeze.contains(worked_day))

    def test_deadline_inside_freeze_window_is_flagged(self):
        freeze = FreezeWindow(START + timedelta(days=2),
                              START + timedelta(days=4), "review window")
        items = [make_item("fix-1", effort=1,
                           deadline=START + timedelta(days=3))]
        result = plan(items, freezes=[freeze])
        self.assertIn(planner.RISK_FREEZE, risk_kinds(result, "fix-1"))
        self.assertIn(planner.WAIT_FREEZE, wait_reasons(result, "fix-1"))

    def test_start_inside_freeze_is_pushed_out(self):
        freeze = FreezeWindow(START, START + timedelta(days=2), "freeze")
        items = [make_item("fix-1", effort=1)]
        result = plan(items, freezes=[freeze])
        entry = scheduled(result, "fix-1")
        self.assertEqual(entry.start, START + timedelta(days=3))


if __name__ == "__main__":
    unittest.main()
