"""Bill Pay Planner - Streamlit app.

Run with:  streamlit run app.py
"""
from __future__ import annotations

import hmac
import json
import time
import uuid
from datetime import date, datetime
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

import data
import engine

st.set_page_config(page_title="Bill Pay Planner", page_icon="🗓️", layout="wide")

# ---------------------------------------------------------------- password

def secret(name: str):
    try:
        return st.secrets.get(name)
    except Exception:  # noqa: BLE001 - no secrets file
        return None


def require_password() -> None:
    """Stop the app until the right password is entered. Fails closed if no password is set."""
    if st.session_state.get("authed"):
        return
    expected = secret("password")
    st.title("Bill Pay Planner")
    if not expected:
        st.error("No password is set, so the planner is locked. Add a line like  password = \"your-password\"  "
                 "to .streamlit/secrets.toml (on your computer) or to the app's Secrets (on Streamlit Cloud).")
        st.stop()
    with st.form("login"):
        pw = st.text_input("Password", type="password")
        ok = st.form_submit_button("Unlock")
    if ok:
        if hmac.compare_digest(pw.encode(), str(expected).encode()):
            st.session_state.authed = True
            st.session_state.fails = 0
            st.rerun()
        st.session_state.fails = st.session_state.get("fails", 0) + 1
        time.sleep(min(10, st.session_state.fails))  # slows down guessing
        st.error("That password isn't right.")
    st.stop()


require_password()

# ---------------------------------------------------------------- state

# Community Cloud runs apps from /mount/src/<repo>
ON_STREAMLIT_CLOUD = Path(__file__).resolve().as_posix().startswith("/mount/src/")

if "storage" not in st.session_state:
    st.session_state.storage = data.make_storage(st.secrets if secret("github") else None)
storage = st.session_state.storage

if "state" not in st.session_state:
    try:
        st.session_state.state = data.load_state(storage)
    except Exception as e:  # noqa: BLE001
        st.error(f"Couldn't load your saved data from {storage.label}: {e}")
        st.stop()
if "rev" not in st.session_state:
    st.session_state.rev = 0  # bumped after each edit so editors start fresh from saved data
state: dict = st.session_state.state


def persist() -> None:
    try:
        data.save_state(storage, st.session_state.state)
        st.session_state.pop("save_error", None)
        st.session_state.just_saved = True
    except Exception as e:  # noqa: BLE001
        st.session_state.save_error = str(e)


def commit() -> None:
    persist()
    st.session_state.rev += 1


def replace_state(new_state: dict) -> None:
    st.session_state.state = data.normalize(new_state)
    for k in [k for k in st.session_state if str(k).startswith("set_")]:
        del st.session_state[k]
    persist()
    st.session_state.rev += 1


# ---------------------------------------------------------------- formatting

def money(x: float, cents: bool = True) -> str:
    s = f"${abs(x):,.2f}" if cents else f"${abs(x):,.0f}"
    return ("−" + s) if x < 0 else s


def long_date(d: date) -> str:
    return f"{d:%a, %b} {d.day}, {d.year}"


def short_date(d: date) -> str:
    return f"{d:%a, %b} {d.day}"


def timing(b: engine.BillPayment, pay_day: date) -> str:
    diff = (b.due - pay_day).days
    if diff > 0:
        text = f"{diff} day{'s' if diff != 1 else ''} early"
    elif diff == 0:
        text = "on due date"
    else:
        text = f"{-diff} day{'s' if diff != -1 else ''} LATE"
    extra = {"manual": " · moved by you", "auto": " · paid early (auto)", "grace": " · late OK"}.get(b.how or "", "")
    return text + extra


# ---------------------------------------------------------------- sidebar

with st.sidebar:
    st.header("Settings")
    s = state["settings"]

    def setting(key: str, widget, label: str, **kw):
        wkey = f"set_{key}"

        def cb():
            state["settings"][key] = st.session_state[wkey]
            commit()

        if widget is st.toggle:
            return widget(label, value=bool(s[key]), key=wkey, on_change=cb, **kw)
        value = int(s[key] or 1) if key == "months" else float(s[key] or 0)
        return widget(label, value=value, key=wkey, on_change=cb, **kw)

    setting("months", st.number_input, "Months to show ahead", min_value=1, max_value=36, step=1)
    setting("start_balance", st.number_input, "Cash on hand at the start", step=100.0, format="%.2f",
            help="Money in the account just before the first paycheck shown. Running totals start here.")
    setting("show_past", st.toggle, "Show past paychecks")
    setting("holiday_shift", st.toggle, "Move paydays off weekends and bank holidays",
            help="Direct deposit lands the business day before.")
    setting("auto_balance", st.toggle, "Pay bills early when a check can't cover its own bills",
            help="Moves bills to the paycheck before, earliest-due first, when that check has room.")
    setting("cushion", st.number_input, "Keep at least this much from each check", min_value=0.0, step=50.0,
            format="%.2f", help="Used with the setting above.")

    st.divider()
    st.subheader("Update from Excel")
    st.caption("Upload your updated Finances.xlsx to replace bills and paychecks. "
               "Paid ticks and moves are kept for bills that keep the same row and name.")
    up = st.file_uploader("Finances workbook", type=["xlsx"], label_visibility="collapsed")
    if up is not None and st.button("Import workbook", use_container_width=True):
        try:
            new = data.import_workbook(up)
            old_names = {b["id"]: b["name"] for b in state["bills"]}
            same = {b["id"] for b in new["bills"] if old_names.get(b["id"]) == b["name"]}
            new["overrides"] = {k: v for k, v in state["overrides"].items() if k.split("|")[0] in same}
            new["paid"] = {k: v for k, v in state["paid"].items() if k.split("|")[0] in same}
            new["settings"] = state["settings"]
            replace_state(new)
            st.success(f"Imported {len(new['bills'])} bills and {len(new['incomes'])} paychecks.")
            st.rerun()
        except Exception as e:  # noqa: BLE001 - show the reason to the user
            st.error(f"Couldn't read that workbook: {e}")

    st.divider()
    st.caption(f"Changes save automatically to {storage.label}.")
    if st.button("Log out", use_container_width=True):
        st.session_state.authed = False
        st.rerun()

    st.divider()
    st.subheader("Backup")
    st.download_button("Download backup (.json)", json.dumps(state, indent=1), file_name="bill_planner_backup.json",
                       mime="application/json", use_container_width=True)
    restore = st.file_uploader("Restore a backup", type=["json"])
    if restore is not None and st.button("Load backup", use_container_width=True):
        try:
            loaded = json.loads(restore.getvalue())
            if not isinstance(loaded.get("bills"), list) or not isinstance(loaded.get("incomes"), list):
                raise ValueError("missing bills or incomes")
            replace_state(loaded)
            st.rerun()
        except Exception as e:  # noqa: BLE001
            st.error(f"That file isn't a planner backup ({e}).")
    confirm = st.checkbox("I want to reset everything")
    if st.button("Reset to spreadsheet values", disabled=not confirm, use_container_width=True):
        replace_state(data.default_state())
        st.rerun()

state = st.session_state.state

# ---------------------------------------------------------------- plan

today = date.today()
plan, shown, cur_day = engine.visible_plan(state, today)
plan_index = {p.date: i for i, p in enumerate(plan)}

st.title("Bill Pay Planner")
st.caption("Which paycheck pays which bill, and what's left over, every payday ahead.")
if st.session_state.get("save_error"):
    st.error(f"Your last change wasn't saved to {storage.label}: {st.session_state.save_error}. "
             "Download a backup from the sidebar to be safe.")
elif st.session_state.pop("just_saved", False):
    st.toast(f"Saved to {storage.label}", icon="✅")
if ON_STREAMLIT_CLOUD and isinstance(storage, data.LocalStorage):
    st.error("Changes are NOT being kept: this app is running on Streamlit Cloud without the [github] section in "
             "its Secrets, so Streamlit erases saved changes whenever the app restarts. Add the [github] lines "
             "(see README.md) under Settings > Secrets.")

if not shown:
    st.info("Add a paycheck on the Paychecks tab to see a schedule.")
else:
    nxt = next((p for p in shown if p.date >= today), shown[-1])
    future = [p for p in shown if cur_day is None or p.date >= cur_day]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Next paycheck", short_date(nxt.date), f"{money(nxt.income - nxt.keep)} net", delta_color="off")
    c2.metric("Bills from it", money(nxt.bills_total),
              f"{sum(1 for b in nxt.bills if b.amount > 0)} bills", delta_color="off")
    c3.metric("Left over from it", money(nxt.left), f"Running total {money(nxt.running)}", delta_color="off")
    if len(future) > 1:
        days = (future[-1].date - future[0].date).days + 14
        avg = sum(p.left for p in future) / (days / 30.44)
        low = min(future, key=lambda p: p.running)
        c4.metric("Average left over per month", money(avg, cents=False),
                  f"Lowest running total {money(low.running, False)} on {low.date:%b} {low.date.day}", delta_color="off")

    if any(p.running < 0 for p in future):
        st.warning("The running total goes below zero. Add your starting cash in the sidebar, or move bills.")
    negs = sum(1 for p in future if p.left < 0)

    # chart
    chart_df = pd.DataFrame({
        "Payday": [p.date.isoformat() for p in shown],
        "Label": [f"{p.date:%b} {p.date.day} '{p.date:%y}" for p in shown],
        "Left over": [round(p.left, 2) for p in shown],
        "Running total": [round(p.running, 2) for p in shown],
        "Bills": [round(p.bills_total, 2) for p in shown],
        "Status": ["Short" if p.left < 0 else "Covered" for p in shown],
    })
    st.subheader("Left over from each paycheck")
    st.caption(f"{negs} paycheck{'s' if negs != 1 else ''} can't cover their own bills and lean on the check before."
               if negs else "Every paycheck covers its own bills.")
    chart = (
        alt.Chart(chart_df)
        .mark_bar(cornerRadiusTopLeft=3, cornerRadiusTopRight=3)
        .encode(
            x=alt.X("Label:N", sort=chart_df["Label"].tolist(), title=None, axis=alt.Axis(labelAngle=-45)),
            y=alt.Y("Left over:Q", title=None, axis=alt.Axis(format="$,.0f")),
            color=alt.Color("Status:N", scale=alt.Scale(domain=["Covered", "Short"], range=["#2a78d6", "#d03b30"]),
                            legend=None),
            tooltip=["Payday", alt.Tooltip("Bills:Q", format="$,.2f"), alt.Tooltip("Left over:Q", format="$,.2f"),
                     alt.Tooltip("Running total:Q", format="$,.2f")],
        )
        .properties(height=260)
    )
    st.altair_chart(chart, use_container_width=True)

tab_sched, tab_bills, tab_pay, tab_how = st.tabs(["Pay schedule", "Bills", "Paychecks", "How your sheet decided"])

# ---------------------------------------------------------------- pay schedule

AUTOMATIC = "Automatic"
pay_labels = {long_date(p.date): p.date for p in plan}
pay_options = [AUTOMATIC] + list(pay_labels)
open_periods: set = st.session_state.setdefault("open_periods", set())


def apply_schedule_edits(ekey: str, bills: list[engine.BillPayment], period: date) -> None:
    """Runs as soon as a cell in a paycheck's table changes; saves right away."""
    edits = st.session_state[ekey].get("edited_rows", {})
    for idx, change in edits.items():
        b = bills[int(idx)]
        if "Paid" in change:
            if change["Paid"]:
                state["paid"][b.key] = True
            else:
                state["paid"].pop(b.key, None)
        if "Amount" in change:
            amt = change["Amount"]
            if amt is None or abs(float(amt) - b.default_amount) < 0.005:
                state["amounts"].pop(b.key, None)          # back to the bill's usual amount
            else:
                state["amounts"][b.key] = round(float(amt), 2)
        if "Pay from" in change:
            choice = change["Pay from"]
            if choice in (None, AUTOMATIC):
                state["overrides"].pop(b.key, None)
            elif choice in pay_labels:
                state["overrides"][b.key] = pay_labels[choice].isoformat()
                open_periods.add(pay_labels[choice])
    open_periods.add(period)  # keep this paycheck open after the page refreshes
    commit()


def note(b: engine.BillPayment) -> str:
    if abs(b.amount - b.default_amount) >= 0.005:
        return f"changed (usually {money(b.default_amount)})"
    if b.amount == 0:
        return "$0: type the amount"
    return ""


with tab_sched:
    st.caption("Through Jun 25, 2027 each paycheck pays exactly the bills in your spreadsheet's C29:C50 formula. "
               "After that the same pattern continues: each check picks up at the next bill row and pays everything "
               "due before the next payday. **Tick Paid** and **type a new Amount** right in the table. Both save "
               "immediately, and an amount you change here applies to that one payment only. To change a bill for "
               "every month, use the Bills tab. To pay a bill from a different paycheck, change **Pay from** "
               "(Automatic undoes it).")
    upcoming = [p.date for p in shown if p.date >= today][:1]
    for p in shown:
        paid_n = sum(1 for b in p.bills if state["paid"].get(b.key))
        left = f":red[Left over {money(p.left)}]" if p.left < 0 else f":green[Left over {money(p.left)}]"
        tag = " · **This paycheck**" if p.date == cur_day else ""
        label = (f"**{long_date(p.date)}**{tag} · Bills {money(p.bills_total)} · {left} · "
                 f"Running {money(p.running)} · {paid_n}/{len(p.bills)} paid")
        with st.expander(label, expanded=(p.date == cur_day or p.date in upcoming or p.date in open_periods)):
            incomes = " + ".join(f"{i['name']} {money(i['amount'])}" for i in p.incomes)
            if p.keep:
                incomes += f" − {money(p.keep)} set aside"
            from_sheet = any(b.how == "sheet" for b in p.bills)
            st.caption(f"{incomes}  ·  Excel: `{engine.excel_formula(p)}`"
                       + ("  (from your sheet)" if from_sheet else "  (pattern continued)"))
            for orig, reason in p.moved_from:
                st.info(f"Deposit moved earlier: {orig:%b} {orig.day} is {reason}.")
            if p.bills:
                df = pd.DataFrame({
                    "Paid": [bool(state["paid"].get(b.key)) for b in p.bills],
                    "Bill": [b.name for b in p.bills],
                    "Row": [b.row for b in p.bills],
                    "Due": [short_date(b.due) for b in p.bills],
                    "Timing": [timing(b, p.date) for b in p.bills],
                    "Amount": [b.amount for b in p.bills],
                    "Note": [note(b) for b in p.bills],
                    "Pay from": [long_date(p.date) if b.how == "manual" else AUTOMATIC for b in p.bills],
                })
                ekey = f"sched_{p.date.isoformat()}_{st.session_state.rev}"
                st.data_editor(
                    df, key=ekey, hide_index=True, use_container_width=True,
                    disabled=["Bill", "Row", "Due", "Timing", "Note"],
                    on_change=apply_schedule_edits, args=(ekey, p.bills, p.date),
                    column_config={
                        "Paid": st.column_config.CheckboxColumn(width="small"),
                        "Row": st.column_config.NumberColumn(width="small", help="The bill's row in your spreadsheet"),
                        "Amount": st.column_config.NumberColumn(min_value=0.0, step=0.01, format="$%.2f",
                                                                help="Change this payment only"),
                        "Pay from": st.column_config.SelectboxColumn(options=pay_options, required=True, width="medium"),
                    },
                )
            else:
                st.caption("No bills from this paycheck.")
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Paycheck", money(p.income - p.keep))
            m2.metric("Bills", money(p.bills_total))
            m3.metric("Left over", money(p.left))
            m4.metric("Running total", money(p.running))

# ---------------------------------------------------------------- bills editor

with tab_bills:
    st.caption("Edit names, due days and amounts here to change a bill for **every month** (payments where you typed "
               "a one-time amount on the Pay schedule keep that amount). Changes save as soon as you leave the cell. "
               "Add rows at the bottom, or select rows and delete them. Keep the bills in due-day order so the "
               "row numbers match your spreadsheet. **Late OK** lets a bill wait for the first paycheck after its "
               "due date, up to that many days.")
    bills_df = pd.DataFrame([{
        "id": b["id"], "Bill": b["name"], "Due day": int(b["day"]), "Amount": float(b["amount"]),
        "Late OK (days)": int(b.get("grace") or 0), "Include": b.get("active", True) is not False,
    } for b in state["bills"]], columns=["id", "Bill", "Due day", "Amount", "Late OK (days)", "Include"])
    edited = st.data_editor(
        bills_df, key=f"bills_{st.session_state.rev}", num_rows="dynamic", hide_index=True, use_container_width=True,
        column_config={
            "id": None,
            "Bill": st.column_config.TextColumn(required=True),
            "Due day": st.column_config.NumberColumn(min_value=1, max_value=31, step=1, required=True),
            "Amount": st.column_config.NumberColumn(min_value=0.0, step=0.01, format="$%.2f"),
            "Late OK (days)": st.column_config.NumberColumn(min_value=0, max_value=20, step=1),
            "Include": st.column_config.CheckboxColumn(),
        },
    )
    new_bills = []
    for _, r in edited.iterrows():
        if not isinstance(r["Bill"], str) or not r["Bill"].strip():
            continue
        new_bills.append({
            "id": r["id"] if isinstance(r["id"], str) and r["id"] else "b" + uuid.uuid4().hex[:6],
            "name": r["Bill"].strip(),
            "day": int(r["Due day"]) if pd.notna(r["Due day"]) else 1,
            "amount": float(r["Amount"]) if pd.notna(r["Amount"]) else 0.0,
            "grace": int(r["Late OK (days)"]) if pd.notna(r["Late OK (days)"]) else 0,
            "active": bool(r["Include"]) if pd.notna(r["Include"]) else True,
        })
    if new_bills != state["bills"]:
        state["bills"] = new_bills
        commit()
        st.rerun()
    st.markdown(f"**Monthly total:** {money(sum(b['amount'] for b in state['bills'] if b.get('active', True)))}")

# ---------------------------------------------------------------- paychecks editor

def to_date(v) -> date:
    if v is None or (not isinstance(v, (date, str)) and pd.isna(v)):
        return today
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


FREQ = {"weekly": "Every week", "biweekly": "Every 2 weeks", "semimonthly": "15th and last day", "monthly": "Monthly"}
FREQ_BACK = {v: k for k, v in FREQ.items()}

with tab_pay:
    st.caption('"Set aside" is your sheet\'s Personal column: money taken off each check before bills.')
    inc_df = pd.DataFrame([{
        "id": x["id"], "Name": x["name"], "Net amount": float(x["amount"]), "Set aside": float(x.get("keep") or 0),
        "First payday": date.fromisoformat(x["first_date"]), "How often": FREQ.get(x["frequency"], FREQ["biweekly"]),
    } for x in state["incomes"]], columns=["id", "Name", "Net amount", "Set aside", "First payday", "How often"])
    inc_edit = st.data_editor(
        inc_df, key=f"inc_{st.session_state.rev}", num_rows="dynamic", hide_index=True, use_container_width=True,
        column_config={
            "id": None,
            "Name": st.column_config.TextColumn(required=True),
            "Net amount": st.column_config.NumberColumn(min_value=0.0, format="$%.2f"),
            "Set aside": st.column_config.NumberColumn(min_value=0.0, format="$%.2f"),
            "First payday": st.column_config.DateColumn(format="MMM D, YYYY"),
            "How often": st.column_config.SelectboxColumn(options=list(FREQ.values())),
        },
    )
    new_inc = []
    for _, r in inc_edit.iterrows():
        if not isinstance(r["Name"], str) or not r["Name"].strip():
            continue
        first = to_date(r["First payday"])
        new_inc.append({
            "id": r["id"] if isinstance(r["id"], str) and r["id"] else "i" + uuid.uuid4().hex[:6],
            "name": r["Name"].strip(),
            "amount": float(r["Net amount"]) if pd.notna(r["Net amount"]) else 0.0,
            "keep": float(r["Set aside"]) if pd.notna(r["Set aside"]) else 0.0,
            "first_date": first.isoformat(),
            "frequency": FREQ_BACK.get(r["How often"], "biweekly"),
        })
    if new_inc != state["incomes"]:
        state["incomes"] = new_inc
        commit()
        st.rerun()

# ---------------------------------------------------------------- explanation

with tab_how:
    st.markdown("""
### How this app decides which paycheck pays which bill
**Through Jun 25, 2027 it uses your spreadsheet's own formulas.** Each payday in C29:C50 subtracts a block of bill
rows, for example `=$H$34-SUM(C26:C27,C2:C13)` on Dec 25. The app pays exactly those rows from that paycheck,
so every number matches your sheet. (The one difference is Sep 4: the sheet used $1,505 for the second paycheck
that day and $1,501 after, and the app uses $1,501, so Sep 4 shows $4 less.)

**After Jun 25, 2027 the same pattern continues.** Your formulas work through the bill list in order: each
paycheck starts at the row after the last one the previous check paid, and wraps from row 27 back to row 2 at
the end of the month. Where your sheet ends, the app keeps going: each check picks up at the next row and pays
every bill due before the following payday, so nothing is paid late. Each paycheck shows its formula in the
same Excel form (`Excel: =Net-SUM(...)`), so you can compare it with your sheet.

### Why the totals swing so much
Paydays are every 14 days but bills are monthly. The check that covers the 1st carries the mortgage and the
other early-month bills (about $3,880 through the 8th), leaving about $67, or less than zero. The other check
carries the 15th–28th bills and keeps $2,100–$3,100. A negative check is covered by the surplus from the check
before it, which is why the app shows a running total.

### What your formulas do that a plain "pay before it's due" rule wouldn't
- **Sep 4** pays September's mortgage (due Sep 1) 3 days late, because the sheet starts after Sep 1.
- **Oct 2** pays the Oct 1 bills (Church, Julian, Cove, Mortgage) one day late instead of from Sep 18.
- **Nov 13, May 14 and Jun 11** pay Hyundai (and NBA in June) early, so the next check, which also carries
  the mortgage, doesn't go negative. Dec, Jan, Feb and Mar don't do this, so those checks go negative.

To change any of this, use **Pay from** on a bill in the Pay schedule. Updating the spreadsheet and importing it
from the sidebar also works: the app reads the new formulas.

### Things worth fixing in the sheet
- Ten bills are $0 (Raymond, Express, both CareCredit accounts, Crossroads School, both Misc., Farm Bureau
  Life, NBA League Pass). If any are real, the leftovers are too high. Type the amounts in the app.
- The summary stops at Jun 25, 2027 even though paydays run to Aug 20, 2027. The app continues it.
- The "Order" tab is left over from a debt-payoff template and shows only #REF! errors.
""")

