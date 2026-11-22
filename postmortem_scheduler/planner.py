"""Dependency-aware scheduler for postmortem action items.

The scheduler consumes action items, dependencies, owner capacity,
deadlines, verification evidence, recurrence-guard levels and freeze
windows, and produces an execution order plus the critical path, risk
items and waiting conditions.

Python 3.8.2 compatible.
"""

import heapq
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import List, Optional

from .models import VERIFY_MANUAL, ActionItem, FreezeWindow, Owner

ONE_DAY = timedelta(days=1)

STATUS_SCHEDULED = "scheduled"
STATUS_SPLIT = "split"
STATUS_DEFERRED = "deferred"
STATUS_BLOCKED = "blocked"

RISK_CYCLE = "dependency_cycle"
RISK_UNKNOWN_DEP = "unknown_dependency"
RISK_DEADLINE = "deadline_missed"
RISK_EVIDENCE = "evidence_missing"
RISK_CAPACITY = "owner_overload"
RISK_FREEZE = "freeze_conflict"

WAIT_CYCLE = "dependency_cycle"
WAIT_DEPENDENCY = "waiting_on_dependency"
WAIT_EVIDENCE = "awaiting_evidence"
WAIT_APPROVAL = "awaiting_approval"
WAIT_CAPACITY = "owner_capacity"
WAIT_FREEZE = "freeze_window"


@dataclass
class ScheduledItem:
    item_id: str
    owner: str
    start: Optional[date]
    end: Optional[date]
    status: str
    note: str = ""


@dataclass
class Risk:
    item_id: str
    kind: str
    message: str


@dataclass
class WaitingCondition:
    item_id: str
    reason: str
    detail: str


@dataclass
class PlanResult:
    order: List[ScheduledItem] = field(default_factory=list)
    critical_path: List[str] = field(default_factory=list)
    risks: List[Risk] = field(default_factory=list)
    waiting: List[WaitingCondition] = field(default_factory=list)
    cycles: List[List[str]] = field(default_factory=list)


def _is_frozen(day, freeze_windows):
    return any(window.contains(day) for window in freeze_windows)


def add_working_days(start, effort_days, freeze_windows):
    """Return the date on which ``effort_days`` of work finishes.

    Work starts on ``start`` (or the first non-frozen day after it) and
    consumes one unit of effort per non-frozen day. Freeze windows are
    skipped entirely.
    """
    freeze_windows = freezes_or_empty(freeze_windows)
    day = start
    while _is_frozen(day, freeze_windows):
        day += ONE_DAY
    remaining = effort_days
    while remaining > 0:
        if not _is_frozen(day, freeze_windows):
            remaining -= 1.0
            if remaining <= 0:
                return day
        day += ONE_DAY
    return day


def freezes_or_empty(freeze_windows):
    return freeze_windows if freeze_windows is not None else []


class Scheduler:
    def __init__(self, items, owners, freeze_windows=None, start_date=None):
        self.items = list(items)
        self.owners = {o.name: o for o in owners}
        self.freeze_windows = list(freeze_windows or [])
        self.start_date = start_date or date.today()

    def plan(self):
        result = PlanResult()
        by_id = {}
        for item in self.items:
            if item.item_id in by_id:
                raise ValueError("duplicate item_id: %r" % item.item_id)
            by_id[item.item_id] = item

        deps = self._resolve_dependencies(by_id, result)
        cyclic = self._find_cycles(by_id, deps, result)
        schedulable = [iid for iid in by_id if iid not in cyclic]
        topo = self._topological_order(by_id, deps, schedulable)
        self._schedule(by_id, deps, topo, result)
        result.critical_path = self._critical_path(by_id, deps, topo)
        return result

    def _resolve_dependencies(self, by_id, result):
        deps = {}
        for item in self.items:
            edges = []
            for dep in item.depends_on:
                if dep not in by_id:
                    result.risks.append(Risk(
                        item.item_id, RISK_UNKNOWN_DEP,
                        "dependency %r does not exist; edge ignored" % dep))
                    continue
                edges.append(dep)
            deps[item.item_id] = edges
        return deps

    def _find_cycles(self, by_id, deps, result):
        """Return the set of item ids that participate in a cycle."""
        index_of = {}
        lowlink = {}
        on_stack = set()
        stack = []
        cyclic = set()
        counter = [0]

        def strongconnect(node):
            # Iterative Tarjan SCC to stay recursion-limit safe.
            work = [(node, iter(deps[node]))]
            index_of[node] = lowlink[node] = counter[0]
            counter[0] += 1
            stack.append(node)
            on_stack.add(node)
            while work:
                current, it = work[-1]
                advanced = False
                for nxt in it:
                    if nxt not in index_of:
                        index_of[nxt] = lowlink[nxt] = counter[0]
                        counter[0] += 1
                        stack.append(nxt)
                        on_stack.add(nxt)
                        work.append((nxt, iter(deps[nxt])))
                        advanced = True
                        break
                    elif nxt in on_stack:
                        lowlink[current] = min(lowlink[current], index_of[nxt])
                if advanced:
                    continue
                work.pop()
                if work:
                    parent = work[-1][0]
                    lowlink[parent] = min(lowlink[parent], lowlink[current])
                if lowlink[current] == index_of[current]:
                    scc = []
                    while True:
                        member = stack.pop()
                        on_stack.discard(member)
                        scc.append(member)
                        if member == current:
                            break
                    is_cycle = len(scc) > 1 or current in deps[current]
                    if is_cycle:
                        cyclic.update(scc)
                        result.cycles.append(sorted(scc))

        for iid in by_id:
            if iid not in index_of:
                strongconnect(iid)

        for scc in result.cycles:
            for iid in scc:
                result.risks.append(Risk(
                    iid, RISK_CYCLE,
                    "item is part of dependency cycle %s" % scc))
                result.waiting.append(WaitingCondition(
                    iid, WAIT_CYCLE,
                    "blocked by dependency cycle %s" % scc))
        return cyclic

    def _topological_order(self, by_id, deps, schedulable):
        """Kahn's algorithm; ready items are prioritised by deadline."""
        schedulable = set(schedulable)
        indegree = {iid: 0 for iid in schedulable}
        followers = {iid: [] for iid in schedulable}
        for iid in schedulable:
            for dep in deps[iid]:
                if dep in schedulable:
                    indegree[iid] += 1
                    followers[dep].append(iid)
        heap = []
        for iid, degree in indegree.items():
            if degree == 0:
                heapq.heappush(heap, (by_id[iid].deadline, iid))
        order = []
        while heap:
            _, iid = heapq.heappop(heap)
            order.append(iid)
            for nxt in followers[iid]:
                indegree[nxt] -= 1
                if indegree[nxt] == 0:
                    heapq.heappush(heap, (by_id[nxt].deadline, nxt))
        return order

    def _schedule(self, by_id, deps, topo, result):
        owner_available = {name: self.start_date for name in self.owners}
        owner_used = {name: 0.0 for name in self.owners}
        finish_of = {}

        for iid in topo:
            item = by_id[iid]
            owner = self.owners.get(item.owner)
            if owner is None:
                result.risks.append(Risk(
                    iid, RISK_CAPACITY,
                    "owner %r has no capacity record" % item.owner))
                result.waiting.append(WaitingCondition(
                    iid, WAIT_CAPACITY,
                    "no capacity record for owner %r" % item.owner))
                result.order.append(ScheduledItem(
                    iid, item.owner, None, None, STATUS_BLOCKED,
                    "unknown owner"))
                continue

            est = max([self.start_date, owner_available[item.owner]] +
                      [finish_of[d] for d in deps[iid] if d in finish_of])
            est = self._next_working_day(est)

            remaining_capacity = owner.capacity_days - owner_used[item.owner]
            effort = item.effort_days
            if effort > remaining_capacity:
                if item.splittable and remaining_capacity > 0:
                    end = add_working_days(est, remaining_capacity,
                                           self.freeze_windows)
                    owner_used[item.owner] = owner.capacity_days
                    owner_available[item.owner] = end + ONE_DAY
                    finish_of[iid] = end
                    leftover = effort - remaining_capacity
                    result.order.append(ScheduledItem(
                        iid, item.owner, est, end, STATUS_SPLIT,
                        "scheduled %.1f of %.1f days; %.1f days deferred"
                        % (remaining_capacity, effort, leftover)))
                    result.risks.append(Risk(
                        iid, RISK_CAPACITY,
                        "owner %r capacity exhausted; %.1f days deferred"
                        % (item.owner, leftover)))
                    result.waiting.append(WaitingCondition(
                        iid, WAIT_CAPACITY,
                        "remaining %.1f days need capacity from owner %r"
                        % (leftover, item.owner)))
                    self._post_schedule_checks(item, end, result)
                    continue
                result.risks.append(Risk(
                    iid, RISK_CAPACITY,
                    "owner %r has %.1f days left but item needs %.1f"
                    % (item.owner, remaining_capacity, effort)))
                result.waiting.append(WaitingCondition(
                    iid, WAIT_CAPACITY,
                    "deferred: owner %r capacity insufficient"
                    % item.owner))
                result.order.append(ScheduledItem(
                    iid, item.owner, None, None, STATUS_DEFERRED,
                    "owner capacity insufficient"))
                self._closure_checks(item, result)
                continue

            end = add_working_days(est, effort, self.freeze_windows)
            owner_used[item.owner] += effort
            owner_available[item.owner] = end + ONE_DAY
            finish_of[iid] = end
            result.order.append(ScheduledItem(
                iid, item.owner, est, end, STATUS_SCHEDULED))
            self._post_schedule_checks(item, end, result)

    def _post_schedule_checks(self, item, end, result):
        if end > item.deadline:
            result.risks.append(Risk(
                item.item_id, RISK_DEADLINE,
                "scheduled finish %s is after deadline %s"
                % (end.isoformat(), item.deadline.isoformat())))
        for window in self.freeze_windows:
            if window.contains(item.deadline):
                result.risks.append(Risk(
                    item.item_id, RISK_FREEZE,
                    "deadline %s falls inside freeze window %s..%s (%s)"
                    % (item.deadline.isoformat(), window.start.isoformat(),
                       window.end.isoformat(), window.reason or "freeze")))
                result.waiting.append(WaitingCondition(
                    item.item_id, WAIT_FREEZE,
                    "deadline inside freeze window ending %s"
                    % window.end.isoformat()))
                break
        self._closure_checks(item, result)

    def _closure_checks(self, item, result):
        if item.closable:
            return
        result.risks.append(Risk(
            item.item_id, RISK_EVIDENCE,
            "no verification evidence; item cannot be closed"))
        if item.verification == VERIFY_MANUAL:
            result.waiting.append(WaitingCondition(
                item.item_id, WAIT_APPROVAL,
                "manual approval and verification evidence required"))
        else:
            result.waiting.append(WaitingCondition(
                item.item_id, WAIT_EVIDENCE,
                "verification evidence required before closure"))

    def _next_working_day(self, day):
        while _is_frozen(day, self.freeze_windows):
            day += ONE_DAY
        return day

    def _critical_path(self, by_id, deps, topo):
        dist = {}
        pred = {}
        for iid in topo:
            best = 0.0
            best_pred = None
            for dep in deps[iid]:
                if dep in dist and dist[dep] > best:
                    best = dist[dep]
                    best_pred = dep
            dist[iid] = best + by_id[iid].effort_days
            pred[iid] = best_pred
        if not dist:
            return []
        tail = max(dist, key=lambda iid: dist[iid])
        path = []
        node = tail
        while node is not None:
            path.append(node)
            node = pred[node]
        path.reverse()
        return path
