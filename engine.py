"""Scheduling engine for the Bill Pay Planner.

Pure functions, no Streamlit. Decides which paycheck pays each bill, in this order:
1. A bill you moved by hand goes to the paycheck you picked.
2. For paydays your spreadsheet covers (the C29:C50 formulas), each paycheck pays exactly
   the bill rows its formula sums, e.g. =$H$34-SUM(C26:C27,C2:C13).
3. After that, the same pattern continues: each paycheck picks up at the next bill row and
   pays every bill due before the next payday (so nothing is paid late).
Optional extras: "late OK" days per bill, and auto-balance (pay early to keep a check positive).
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
    key: str          # "<bill id>|<due date>" - stable id for overrides and paid ticks
    bill_id: str
    name: str
    amount: float
    due: date
    how: str | None = None   # None (continued pattern), "sheet", "manual", "auto", "grace"
    default_amount: float = 0.0  # the bill's usual amount; amount differs when edited for this payment
    row: int = 0                 # the bill's row number in the spreadsheet layout (row 2 = first bill)


@dataclass
class Paycheck:
    date: date
    sched: date | None = None    # the regular payday before any holiday/weekend shift
    incomes: list[dict] = field(default_factory=list)
    moved_from: list[tuple[date, str]] = field(default_factory=list)
    bills: list[BillPayment] = field(default_factory=list)
    income: float = 0.0
    keep: float = 0.0
    bills_total: float = 0.0
    left: float = 0.0
    running: float = 0.0


def build_plan(state: dict, end: date) -> list[Paycheck]:
    s = state["settings"]

    # 1. paydays
    by_date: dict[date, Paycheck] = {}
    for inc in state["incomes"]:
        if not inc.get("first_date") or not (float(inc.get("amount") or 0) > 0):
            continue
        for sched in schedule_dates(inc, end + timedelta(days=45)):
            dep, reason = deposit_date(sched, bool(s.get("holiday_shift", True)))
            p = by_date.setdefault(dep, Paycheck(date=dep, sched=sched))
            p.sched = min(p.sched or sched, sched)
            p.incomes.append({"name": inc["name"], "amount": float(inc["amount"] or 0),
                              "keep": float(inc.get("keep") or 0)})
            if reason:
                p.moved_from.append((sched, reason))
    plan = sorted(by_date.values(), key=lambda p: p.date)
    if not plan:
        return []
    for p in plan:
        p.income = sum(i["amount"] for i in p.incomes)
        p.keep = sum(i["keep"] for i in p.incomes)
    index_of = {p.date: i for i, p in enumerate(plan)}

    # 2. each bill occurrence -> a paycheck
    overrides: dict = state.get("overrides", {})
    amounts: dict = state.get("amounts", {})
    bills_by_id = {b["id"]: b for b in state["bills"] if b.get("active") is not False}
    row_of = {b["id"]: i + 2 for i, b in enumerate(state["bills"])}

    def occurrence(b: dict, on_or_after: date) -> date:
        y, m = on_or_after.year, on_or_after.month
        for _ in range(3):
            d = date(y, m, max(1, min(int(b.get("day") or 1), calendar.monthrange(y, m)[1])))
            if d >= on_or_after:
                return d
            y, m = add_months(y, m, 1)
        return d

    # 2a. paydays your sheet covers: the exact rows in each summary formula
    sheet_assigned: dict[str, int] = {}
    by_sched = {p.sched: i for i, p in enumerate(plan)}
    for entry in state.get("sheet_plan", []):
        i = by_sched.get(date.fromisoformat(entry["date"]))
        if i is None:
            continue
        for bid in entry["bills"]:
            b = bills_by_id.get(bid)
            if not b:
                continue
            # the formula's row means the next time that bill comes due (allowing a few days late)
            due = occurrence(b, plan[i].sched - timedelta(days=7))
            sheet_assigned.setdefault(f"{bid}|{due.isoformat()}", i)

    # 2b. every bill, every month
    y, m = plan[0].date.year, plan[0].date.month
    last = plan[-1].date
    while date(y, m, 1) <= last:
        dim = calendar.monthrange(y, m)[1]
        for b in state["bills"]:
            if b.get("active") is False:
                continue
            day = max(1, min(int(b.get("day") or 1), dim))
            due = date(y, m, day)
            key = f"{b['id']}|{due.isoformat()}"
            idx, how = -1, None
            ov = overrides.get(key)
            if ov and date.fromisoformat(ov) in index_of:
                idx, how = index_of[date.fromisoformat(ov)], "manual"
            elif key in sheet_assigned:
                idx, how = sheet_assigned[key], "sheet"
            else:
                for i in range(len(plan) - 1, -1, -1):
                    if plan[i].date <= due:
                        idx = i
                        break
                grace = int(b.get("grace") or 0)
                if grace > 0:
                    for i, p in enumerate(plan):
                        if due < p.date <= due + timedelta(days=grace):
                            idx, how = i, "grace"
                            break
            if idx < 0:
                continue  # due before the first paycheck on record
            default = float(b.get("amount") or 0)
            amt = float(amounts[key]) if key in amounts else default
            plan[idx].bills.append(BillPayment(key, b["id"], b["name"], amt, due, how, default, row_of[b["id"]]))
        y, m = add_months(y, m, 1)

    def recompute(p: Paycheck) -> None:
        p.bills_total = sum(x.amount for x in p.bills)
        p.left = p.income - p.keep - p.bills_total

    for p in plan:
        recompute(p)

    # 3. optional auto-balance: pay a tight check's bills early from the check before it
    if s.get("auto_balance"):
        cushion = float(s.get("cushion") or 0)
        for i in range(1, len(plan)):
            cur, prev = plan[i], plan[i - 1]
            if cur.left >= cushion:
                continue
            movable = sorted((x for x in cur.bills if not x.how and x.amount > 0), key=lambda x: (x.due, x.amount))
            for x in movable:
                if cur.left >= cushion:
                    break
                if prev.left - x.amount < cushion:
                    continue
                cur.bills.remove(x)
                x.how = "auto"
                prev.bills.append(x)
                recompute(cur)
                recompute(prev)

    for p in plan:
        p.bills.sort(key=lambda x: (x.due, x.row))
        recompute(p)
    return plan


def visible_plan(state: dict, today: date) -> tuple[list[Paycheck], list[Paycheck], date | None]:
    """Returns (full plan, paychecks to show with running totals, current payday)."""
    s = state["settings"]
    months = max(1, min(36, int(s.get("months") or 12)))
    ey, em = add_months(today.year, today.month, months)
    end = date(ey, em, min(today.day, calendar.monthrange(ey, em)[1]))
    plan = build_plan(state, end)
    cur_idx = max((i for i, p in enumerate(plan) if p.date <= today), default=-1)
    start = 0 if s.get("show_past") else max(0, cur_idx)
    shown = [p for i, p in enumerate(plan) if i >= start and p.date <= end]
    run = float(s.get("start_balance") or 0)
    for p in shown:
        run += p.left
        p.running = run
    return plan, shown, (plan[cur_idx].date if cur_idx >= 0 else None)


def excel_formula(p: Paycheck) -> str:
    """The paycheck written the way the spreadsheet's summary formulas are, e.g. =Net-SUM(C26:C27,C2:C13)."""
    rows = [b.row for b in sorted(p.bills, key=lambda b: (b.due, b.row))]
    if not rows:
        return "=Net"
    parts, start, prev = [], rows[0], rows[0]
    for r in rows[1:] + [None]:
        if r is not None and r == prev + 1:
            prev = r
            continue
        parts.append(f"C{start}" if start == prev else f"C{start}:C{prev}")
        if r is not None:
            start = prev = r
    return "=Net-SUM(" + ",".join(parts) + ")"
