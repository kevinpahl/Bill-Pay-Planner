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
    "version": 3,
    "incomes": [
        {"id": "i1", "name": "Paycheck 1", "amount": 2445.0, "keep": 0.0, "first_date": "2026-09-04", "frequency": "biweekly"},
        {"id": "i2", "name": "Paycheck 2", "amount": 1501.0, "keep": 0.0, "first_date": "2026-09-04", "frequency": "biweekly"},
    ],
    "bills": [
        {"id": f"b{i + 1}", "name": n, "day": d, "amount": float(a), "active": True}
        for i, (n, d, a) in enumerate(_BILLS)
    ],
    "settings": {"months": 12, "start_balance": 0.0, "holiday_shift": False, "show_past": False},
    "amounts": {},     # amount changed for one payment only: "<bill id>|<due date>" -> amount
    "paid": {},        # "<bill id>|<due date>" -> True
}

def default_state() -> dict:
    return copy.deepcopy(DEFAULT_STATE)


def normalize(s: dict) -> dict:
    d = default_state()
    s.setdefault("incomes", [])
    s.setdefault("bills", [])
    s["settings"] = {**d["settings"], **s.get("settings", {})}
    s.setdefault("amounts", {})
    s.setdefault("paid", {})
    if int(s.get("version", 1)) < 3:
        # Older versions used the spreadsheet's formulas and hand moves. The app now follows one rule
        # (each check pays only the bills due before the next payday), so those are dropped.
        # Paid ticks and changed amounts are kept.
        for k in ("sheet_plan", "overrides"):
            s.pop(k, None)
        for k in ("auto_balance", "cushion"):
            s["settings"].pop(k, None)
        s["settings"]["holiday_shift"] = False
        s["version"] = 3
    return s


class LocalStorage:
    """Saves to planner_data.json next to the app. Good for running on your own computer."""
    label = "this computer"

    def load(self) -> dict | None:
        if DATA_FILE.exists():
            try:
                return json.loads(DATA_FILE.read_text())
            except (json.JSONDecodeError, OSError):
                return None
        return None

    def save(self, state: dict) -> None:
        tmp = DATA_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1))
        tmp.replace(DATA_FILE)


class GitHubStorage:
    """Saves to a JSON file on a separate branch of your private GitHub repo.

    Streamlit Community Cloud erases files the app writes whenever it restarts, so on the cloud
    the data lives in GitHub instead. A separate branch keeps these saves from redeploying the app.
    """
    label = "GitHub"
    API = "https://api.github.com"

    def __init__(self, token: str, repo: str, branch: str = "planner-data", path: str = "planner_data.json"):
        import requests  # installed with Streamlit

        self.requests = requests
        self.repo, self.branch, self.path = repo, branch, path
        self.headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28"}
        self.sha: str | None = None

    def _url(self) -> str:
        return f"{self.API}/repos/{self.repo}/contents/{self.path}"

    def _check(self, r, action: str) -> None:
        if r.status_code >= 400:
            raise RuntimeError(f"GitHub {action} failed ({r.status_code}): {r.text[:200]}")

    def load(self) -> dict | None:
        import base64

        r = self.requests.get(self._url(), headers=self.headers, params={"ref": self.branch}, timeout=15)
        if r.status_code == 404:
            return None  # no saved data yet, or the branch doesn't exist yet
        self._check(r, "load")
        body = r.json()
        self.sha = body["sha"]
        return json.loads(base64.b64decode(body["content"]))

    def _ensure_branch(self) -> None:
        r = self.requests.get(f"{self.API}/repos/{self.repo}/git/ref/heads/{self.branch}", headers=self.headers, timeout=15)
        if r.status_code == 200:
            return
        repo = self.requests.get(f"{self.API}/repos/{self.repo}", headers=self.headers, timeout=15)
        self._check(repo, "repo lookup")
        default = repo.json()["default_branch"]
        ref = self.requests.get(f"{self.API}/repos/{self.repo}/git/ref/heads/{default}", headers=self.headers, timeout=15)
        self._check(ref, "branch lookup")
        made = self.requests.post(f"{self.API}/repos/{self.repo}/git/refs", headers=self.headers, timeout=15,
                                  json={"ref": f"refs/heads/{self.branch}", "sha": ref.json()["object"]["sha"]})
        if made.status_code != 422:  # 422 = someone else just created it
            self._check(made, "branch create")

    def save(self, state: dict) -> None:
        import base64

        content = base64.b64encode(json.dumps(state, indent=1).encode()).decode()
        for attempt in range(2):
            body = {"message": "Update bill planner data", "content": content, "branch": self.branch}
            if self.sha:
                body["sha"] = self.sha
            r = self.requests.put(self._url(), headers=self.headers, json=body, timeout=15)
            if r.status_code in (404, 422) and attempt == 0:
                # branch missing, or our copy is out of date: fix and try once more
                self._ensure_branch()
                self.sha = None
                existing = self.requests.get(self._url(), headers=self.headers, params={"ref": self.branch}, timeout=15)
                if existing.status_code == 200:
                    self.sha = existing.json()["sha"]
                continue
            if r.status_code == 409 and attempt == 0:
                self.sha = None
                existing = self.requests.get(self._url(), headers=self.headers, params={"ref": self.branch}, timeout=15)
                if existing.status_code == 200:
                    self.sha = existing.json()["sha"]
                continue
            self._check(r, "save")
            self.sha = r.json()["content"]["sha"]
            return


def make_storage(secrets) -> LocalStorage | GitHubStorage:
    """Use GitHub when a [github] section is in secrets, otherwise the local file."""
    try:
        gh = secrets.get("github") if secrets is not None else None
    except Exception:  # noqa: BLE001 - no secrets file at all
        gh = None
    if gh and gh.get("token") and gh.get("repo"):
        return GitHubStorage(gh["token"], gh["repo"], gh.get("branch", "planner-data"), gh.get("path", "planner_data.json"))
    return LocalStorage()


def load_state(storage) -> dict:
    loaded = storage.load()
    return normalize(loaded) if loaded else default_state()


def save_state(storage, state: dict) -> None:
    storage.save(state)


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
        bid = f"b{len(bills) + 1}"
        bills.append({"id": bid, "name": name.strip(), "day": int(day),
                      "amount": float(amt or 0), "active": True})
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
    state["amounts"], state["paid"] = {}, {}
    return state
