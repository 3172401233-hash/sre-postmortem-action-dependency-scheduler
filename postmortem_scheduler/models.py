"""Domain model for postmortem action-item scheduling.

Python 3.8.2 compatible. Owners are identified by opaque role tokens
(for example ``"owner-alpha"``); never store real personnel data here.
"""

from dataclasses import dataclass, field
from datetime import date
from typing import List

KIND_FIX = "fix"
KIND_VERIFY = "verify"
KIND_APPROVE = "approve"

VERIFY_AUTO = "auto"
VERIFY_MANUAL = "manual"

GUARD_LOW = "low"
GUARD_MEDIUM = "medium"
GUARD_HIGH = "high"
GUARD_LEVELS = (GUARD_LOW, GUARD_MEDIUM, GUARD_HIGH)


@dataclass
class ActionItem:
    """A single follow-up action from a postmortem review."""

    item_id: str
    title: str
    owner: str
    effort_days: float
    deadline: date
    kind: str = KIND_FIX
    depends_on: List[str] = field(default_factory=list)
    verification: str = VERIFY_MANUAL
    verification_evidence: List[str] = field(default_factory=list)
    recurrence_guard: str = GUARD_MEDIUM
    splittable: bool = False

    def __post_init__(self):
        if self.kind not in (KIND_FIX, KIND_VERIFY, KIND_APPROVE):
            raise ValueError("unknown kind: %r" % self.kind)
        if self.verification not in (VERIFY_AUTO, VERIFY_MANUAL):
            raise ValueError("unknown verification: %r" % self.verification)
        if self.recurrence_guard not in GUARD_LEVELS:
            raise ValueError("unknown recurrence_guard: %r" % self.recurrence_guard)
        if self.effort_days < 0:
            raise ValueError("effort_days must be >= 0")

    @property
    def closable(self):
        """An item can only close with evidence, unless auto-verified."""
        if self.verification == VERIFY_AUTO:
            return True
        return bool(self.verification_evidence)


@dataclass
class Owner:
    """An assignee with a capacity budget (working days in the horizon)."""

    name: str
    capacity_days: float

    def __post_init__(self):
        if self.capacity_days < 0:
            raise ValueError("capacity_days must be >= 0")


@dataclass
class FreezeWindow:
    """A change-freeze / review window during which no work is scheduled."""

    start: date
    end: date
    reason: str = ""

    def __post_init__(self):
        if self.end < self.start:
            raise ValueError("freeze window end before start")

    def contains(self, day):
        return self.start <= day <= self.end
