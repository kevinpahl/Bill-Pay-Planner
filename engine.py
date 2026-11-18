"""Scheduling engine for the Bill Pay Planner.

Pure functions, no Streamlit. One rule decides which paycheck pays each bill:

    A paycheck pays only the bills that come due from its payday up to the day
    before the next payday. Bills due later wait for the check they fall under.

Money a check doesn't spend carries over to the next check, so a check whose
bills are bigger than the paycheck uses what the earlier checks left behind.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date, timedelta

# ---------------------------------------------------------------- holidays

def _nth_weekday(y: int, m: int, weekday: int, n: int) -> date:
    d = date(y, m, 1)
    while d.weekday() != weekday:
        d += timedelta(days=1)
    return d + timedelta(weeks=n - 1)


def _last_weekday(y: int, m: int, weekday: int) -> date:
    d = date(y, m, calendar.monthrange(y, m)[1])
    while d.weekday() != weekday:
        d -= timedelta(days=1)
    return d


_holiday_cache: dict[int, dict[date, str]] = {}


def holidays_for(y: int) -> dict[date, str]:
    """US federal (bank) holidays for year y, using the observed date."""
    if y in _holiday_cache:
        return _holiday_cache[y]
    out: dict[date, str] = {}
    for m, d, name in [(1, 1, "New Year's Day"), (6, 19, "Juneteenth"), (7, 4, "Independence Day"),
                       (11, 11, "Veterans Day"), (12, 25, "Christmas")]:
        t = date(y, m, d)
        if t.weekday() == 5:      # Saturday -> Friday
            t -= timedelta(days=1)
        elif t.weekday() == 6:    # Sunday -> Monday
            t += timedelta(days=1)
        out[t] = name
    out[_nth_weekday(y, 1, 0, 3)] = "MLK Day"
    out[_nth_weekday(y, 2, 0, 3)] = "Presidents' Day"
    out[_last_weekday(y, 5, 0)] = "Memorial Day"
    out[_nth_weekday(y, 9, 0, 1)] = "Labor Day"
    out[_nth_weekday(y, 10, 0, 2)] = "Columbus Day"
    out[_nth_weekday(y, 11, 3, 4)] = "Thanksgiving"
    _holiday_cache[y] = out
    return out


def holiday_name(d: date) -> str | None:
    return holidays_for(d.year).get(d) or holidays_for(d.year + 1).get(d)


def deposit_date(d: date, adjust: bool) -> tuple[date, str | None]:
    """Direct deposit lands the business day before a weekend or bank holiday."""
    if not adjust:
        return d, None
    reason = None
    for _ in range(7):
        h = holiday_name(d)
        if h:
            reason = reason or h
            d -= timedelta(days=1)
            continue
        if d.weekday() >= 5:
            reason = reason or "a weekend"
            d -= timedelta(days=1)
            continue
        break
    return d, reason

# ---------------------------------------------------------------- paydays

def add_months(y: int, m: int, n: int) -> tuple[int, int]:
    m0 = m - 1 + n
    return y + m0 // 12, m0 % 12 + 1


def schedule_dates(inc: dict, end: date) -> list[date]:
    first = date.fromisoformat(str(inc["first_date"]))
    freq = inc.get("frequency", "biweekly")
    out: list[date] = []
    if freq in ("weekly", "biweekly"):
        step = timedelta(days=7 if freq == "weekly" else 14)
        t = first
        while t <= end:
            out.append(t)
            t += step
        return out
    y, m = first.year, first.month
    while True:
        dim = calendar.monthrange(y, m)[1]
        days = [15, dim] if freq == "semimonthly" else [min(first.day, dim)]
        for d in days:
            t = date(y, m, d)
            if t > end:
                return out
            if t >= first:
                out.append(t)
        y, m = add_months(y, m, 1)

# ---------------------------------------------------------------- plan

@dataclass
class BillPayment:
    key: str             # "<bill id>|<due date>": stable id for paid ticks and amount changes
    bill_id: str
    name: str
    amount: float
    due: date
    default_amount: float = 0.0   # the bill's usual amount; differs when changed for this payment


@dataclass
class Paycheck:
    date: date
    next_date: date | None = None        # the following payday
    incomes: list[dict] = field(default_factory=list)
    moved_from: list[tuple[date, str]] = field(default_factory=list)
    bills: list[BillPayment] = field(default_factory=list)
    income: float = 0.0
    keep: float = 0.0
    bills_total: float = 0.0
    left: float = 0.0          # this paycheck minus its own bills
    carried_in: float = 0.0    # left over from earlier checks
    carried_out: float = 0.0   # carried_in + left: what goes on to the next check

    @property
    def window_end(self) -> date:
        """Last due date this check pays (the day before the next payday)."""
        return (self.next_date or self.date + timedelta(days=14)) - timedelta(days=1)

    @property
    def available(self) -> float:
        return self.carried_in + self.income - self.keep


def build_plan(state: dict, end: date) -> list[Paycheck]:
    s = state["settings"]

    # 1. paydays
    by_date: dict[date, Paycheck] = {}
    for inc in state["incomes"]:
        if not inc.get("first_date") or not (float(inc.get("amount") or 0) > 0):
            continue
        for sched in schedule_dates(inc, end + timedelta(days=45)):
            dep, reason = deposit_date(sched, bool(s.get("holiday_shift", False)))
            p = by_date.setdefault(dep, Paycheck(date=dep))
            p.incomes.append({"name": inc["name"], "amount": float(inc["amount"] or 0),
                              "keep": float(inc.get("keep") or 0)})
            if reason:
                p.moved_from.append((sched, reason))
    plan = sorted(by_date.values(), key=lambda p: p.date)
    if not plan:
        return []
    for i, p in enumerate(plan):
        p.income = sum(x["amount"] for x in p.incomes)
        p.keep = sum(x["keep"] for x in p.incomes)
        p.next_date = plan[i + 1].date if i + 1 < len(plan) else None

    # 2. every bill, every month -> the check whose window holds its due date
    amounts: dict = state.get("amounts", {})
    y, m = plan[0].date.year, plan[0].date.month
    last = plan[-1].date
    j = 0
    occurrences = []
    while date(y, m, 1) <= last:
        dim = calendar.monthrange(y, m)[1]
        for order, b in enumerate(state["bills"]):
            if b.get("active") is False:
                continue
            due = date(y, m, max(1, min(int(b.get("day") or 1), dim)))
            occurrences.append((due, order, b))
        y, m = add_months(y, m, 1)
    occurrences.sort(key=lambda t: (t[0], t[1]))
    for due, _, b in occurrences:
        if due < plan[0].date or due > plan[-1].window_end:
            continue  # before the first paycheck on record, or past the last one
        while plan[j].window_end < due:
            j += 1
        key = f"{b['id']}|{due.isoformat()}"
        default = float(b.get("amount") or 0)
        amt = float(amounts[key]) if key in amounts else default
        plan[j].bills.append(BillPayment(key, b["id"], b["name"], amt, due, default))

    for p in plan:
        p.bills_total = sum(x.amount for x in p.bills)
        p.left = p.income - p.keep - p.bills_total
    return plan


def visible_plan(state: dict, today: date) -> tuple[list[Paycheck], list[Paycheck], date | None]:
    """Returns (full plan, paychecks to show with carry-over filled in, current payday)."""
    s = state["settings"]
    months = max(1, min(36, int(s.get("months") or 12)))
    ey, em = add_months(today.year, today.month, months)
    end = date(ey, em, min(today.day, calendar.monthrange(ey, em)[1]))
    plan = build_plan(state, end)
    cur_idx = max((i for i, p in enumerate(plan) if p.date <= today), default=-1)
    start = 0 if s.get("show_past") else max(0, cur_idx)
    shown = [p for i, p in enumerate(plan) if i >= start and p.date <= end]
    carry = float(s.get("start_balance") or 0)
    for p in shown:
        p.carried_in = carry
        p.carried_out = carry + p.left
        carry = p.carried_out
    return plan, shown, (plan[cur_idx].date if cur_idx >= 0 else None)
