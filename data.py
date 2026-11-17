"""Default data (from Finances.xlsx), saving/loading, and re-importing the spreadsheet."""
from __future__ import annotations

import copy
import json
from datetime import date, datetime
from pathlib import Path

DATA_FILE = Path(__file__).with_name("planner_data.json")

_BILLS = [
    ("Church", 1, 200), ("Julian", 1, 200), ("Raymond", 1, 0), ("Cove", 1, 17.99), ("Mortgage", 1, 3200),
    ("Express", 1, 0), ("Holly CareCredit", 1, 0), ("YouTube TV", 2, 88.59), ("Starlink", 3, 130), ("Google", 4, 12.8),
    ("Misc.", 5, 0), ("Microsoft", 7, 13.87), ("Amazon", 8, 16), ("Pontoon", 15, 369), ("Farm Bureau Car", 15, 211.93),
    ("Church", 15, 200), ("Julian", 15, 200), ("Farm Bureau Boats", 20, 25), ("Crossroads School", 20, 0), ("Misc.", 20, 0),
    ("Farm Bureau Life", 21, 0), ("Kevin CareCredit", 21, 0), ("Duke Power", 22, 300), ("T-Mobile", 24, 157.08),
    ("NBA League Pass", 26, 0), ("Hyundai", 28, 340),
]

DEFAULT_STATE = {
    "version": 1,
    "incomes": [
        {"id": "i1", "name": "Paycheck 1", "amount": 2445.0, "keep": 0.0, "first_date": "2026-09-04", "frequency": "biweekly"},
        {"id": "i2", "name": "Paycheck 2", "amount": 1501.0, "keep": 0.0, "first_date": "2026-09-04", "frequency": "biweekly"},
    ],
    "bills": [
        {"id": f"b{i + 1}", "name": n, "day": d, "amount": float(a), "grace": 0, "active": True}
        for i, (n, d, a) in enumerate(_BILLS)
    ],
    "settings": {"months": 12, "start_balance": 0.0, "auto_balance": False, "cushion": 0.0,
                 "holiday_shift": True, "show_past": False},
    # Moves you made by hand in the spreadsheet: Hyundai (and NBA in June) paid from the earlier check.
    "overrides": {"b26|2026-11-28": "2026-11-13", "b26|2027-05-28": "2027-05-14",
                  "b25|2027-06-26": "2027-06-11", "b26|2027-06-28": "2027-06-11"},
    "paid": {},
}


def default_state() -> dict:
    return copy.deepcopy(DEFAULT_STATE)


def normalize(s: dict) -> dict:
    d = default_state()
    s.setdefault("incomes", [])
    s.setdefault("bills", [])
    s["settings"] = {**d["settings"], **s.get("settings", {})}
    s.setdefault("overrides", {})
    s.setdefault("paid", {})
    return s


def load_state() -> dict:
    if DATA_FILE.exists():
        try:
            return normalize(json.loads(DATA_FILE.read_text()))
        except (json.JSONDecodeError, OSError):
            pass
    return default_state()


def save_state(state: dict) -> None:
    tmp = DATA_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(DATA_FILE)


def import_workbook(file) -> dict:
    """Read bills and paychecks from a workbook laid out like Finances.xlsx.

    Bills: rows where column A is the due day, B the name and C the amount (until the first blank day).
    Paychecks: first payday in E8, paycheck 1 in F11 (gross) / G11 (personal), paycheck 2 in F12 / G12.
    """
    import openpyxl

    wb = openpyxl.load_workbook(file, data_only=True)
    ws = wb["Finances"] if "Finances" in wb.sheetnames else wb.worksheets[0]
    bills = []
    for r in range(2, ws.max_row + 1):
        day, name, amt = ws.cell(r, 1).value, ws.cell(r, 2).value, ws.cell(r, 3).value
        if not isinstance(day, (int, float)) or not isinstance(name, str):
            if bills:
                break
            continue
        bills.append({"id": f"b{len(bills) + 1}", "name": name.strip(), "day": int(day),
                      "amount": float(amt or 0), "grace": 0, "active": True})
    if not bills:
        raise ValueError("No bills found. Expected due day in column A, name in B and amount in C.")

    first = ws["E8"].value
    if isinstance(first, datetime):
        first = first.date()
    if not isinstance(first, date):
        raise ValueError("Couldn't find the first payday in cell E8.")

    def num(ref: str) -> float:
        v = ws[ref].value
        return float(v) if isinstance(v, (int, float)) else 0.0

    incomes = []
    for i, (f, g) in enumerate([("F11", "G11"), ("F12", "G12")], start=1):
        if num(f) > 0:
            incomes.append({"id": f"i{i}", "name": f"Paycheck {i}", "amount": num(f), "keep": num(g),
                            "first_date": first.isoformat(), "frequency": "biweekly"})
    state = default_state()
    state["bills"], state["incomes"] = bills, incomes or state["incomes"]
    state["overrides"], state["paid"] = {}, {}
    return state
