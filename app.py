"""Bill Pay Planner - Streamlit app.

Run with:  streamlit run app.py
"""
from __future__ import annotations

import hmac
import json
import time
import uuid
from datetime import date, datetime, timedelta
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


def md(d: date) -> str:
    return f"{d:%b} {d.day}"


def days_after(b: engine.BillPayment, pay_day: date) -> str:
    n = (b.due - pay_day).days
    return "on payday" if n == 0 else f"{n} day{'s' if n != 1 else ''} after payday"


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
    setting("start_balance", st.number_input, "Money already in the account", step=100.0, format="%.2f",
            help="What's in the account just before the first paycheck shown. It's carried into that check.")
    setting("show_past", st.toggle, "Show past paychecks")
    setting("holiday_shift", st.toggle, "Move paydays off weekends and bank holidays",
            help="Direct deposit lands the business day before.")

    st.divider()
    st.subheader("Update from Excel")
    st.caption("Upload your updated Finances.xlsx to replace bills and paychecks. "
               "Paid ticks and changed amounts are kept for bills that keep the same row and name.")
    up = st.file_uploader("Finances workbook", type=["xlsx"], label_visibility="collapsed")
    if up is not None and st.button("Import workbook", use_container_width=True):
        try:
            new = data.import_workbook(up)
            old_names = {b["id"]: b["name"] for b in state["bills"]}
            same = {b["id"] for b in new["bills"] if old_names.get(b["id"]) == b["name"]}
            new["amounts"] = {k: v for k, v in state["amounts"].items() if k.split("|")[0] in same}
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
next_of = {p.date: (plan[i + 1] if i + 1 < len(plan) else None) for i, p in enumerate(plan)}

st.title("Bill Pay Planner")
st.caption("Each paycheck pays only the bills due before the next payday. Whatever is left carries over to the next check.")
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
    cur = next((p for p in shown if p.date == cur_day), shown[0])
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Current paycheck", short_date(cur.date), f"pays bills due {md(cur.date)} – {md(cur.window_end)}",
              delta_color="off")
    c2.metric("Bills due before the next check", money(cur.bills_total),
              f"{sum(1 for b in cur.bills if not state['paid'].get(b.key))} still to pay", delta_color="off")
    c3.metric("Carried to the next check", money(cur.carried_out),
              f"next check {short_date(cur.next_date)}" if cur.next_date else None, delta_color="off")
    low = min(shown, key=lambda p: p.carried_out)
    c4.metric("Lowest point ahead", money(low.carried_out, cents=False), f"after the {md(low.date)} check",
              delta_color="off")
    if low.carried_out < 0:
        st.warning(f"After the {md(low.date)} check the money runs out by {money(-low.carried_out)}. "
                   "Enter what's already in the account in the sidebar, or lower some bills.")

    chart_df = pd.DataFrame({
        "Payday": [long_date(p.date) for p in shown],
        "Label": [f"{p.date:%b} {p.date.day} '{p.date:%y}" for p in shown],
        "Pays bills due": [f"{md(p.date)} – {md(p.window_end)}" for p in shown],
        "Bills": [round(p.bills_total, 2) for p in shown],
        "This check's leftover": [round(p.left, 2) for p in shown],
        "Carried to next check": [round(p.carried_out, 2) for p in shown],
        "Status": ["Short" if p.carried_out < 0 else "OK" for p in shown],
    })
    st.subheader("Money carried to the next check")
    chart = (
        alt.Chart(chart_df)
        .mark_bar(cornerRadiusTopLeft=3, cornerRadiusTopRight=3)
        .encode(
            x=alt.X("Label:N", sort=chart_df["Label"].tolist(), title=None, axis=alt.Axis(labelAngle=-45)),
            y=alt.Y("Carried to next check:Q", title=None, axis=alt.Axis(format="$,.0f")),
            color=alt.Color("Status:N", scale=alt.Scale(domain=["OK", "Short"], range=["#2a78d6", "#d03b30"]),
                            legend=None),
            tooltip=["Payday", "Pays bills due", alt.Tooltip("Bills:Q", format="$,.2f"),
                     alt.Tooltip("This check's leftover:Q", format="$,.2f"),
                     alt.Tooltip("Carried to next check:Q", format="$,.2f")],
        )
        .properties(height=240)
    )
    st.altair_chart(chart, use_container_width=True)

tab_sched, tab_bills, tab_pay, tab_how = st.tabs(["Pay schedule", "Bills", "Paychecks", "How it works"])

# ---------------------------------------------------------------- pay schedule

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
    open_periods.add(period)  # keep this paycheck open after the page refreshes
    commit()


def note(b: engine.BillPayment) -> str:
    if abs(b.amount - b.default_amount) >= 0.005:
        return f"changed (usually {money(b.default_amount)})"
    if b.amount == 0:
        return "$0: type the amount"
    return ""


with tab_sched:
    st.caption("Each paycheck pays only the bills due from its payday up to the day before the next payday. "
               "Bills due later wait for the check they fall under, and leftover money carries forward. "
               "**Tick Paid** as you pay, and **type over an Amount** to change that one payment. Both save "
               "immediately. To change a bill for every month, use the Bills tab.")
    for p in shown:
        nxt = next_of.get(p.date)
        paid_n = sum(1 for b in p.bills if state["paid"].get(b.key))
        carry = (f":red[Carry to next {money(p.carried_out)}]" if p.carried_out < 0
                 else f":green[Carry to next {money(p.carried_out)}]")
        tag = " · **Current**" if p.date == cur_day else ""
        label = (f"**{long_date(p.date)}**{tag} · pays bills due {md(p.date)}–{md(p.window_end)} · "
                 f"Bills {money(p.bills_total)} · {carry} · {paid_n}/{len(p.bills)} paid")
        with st.expander(label, expanded=(p.date == cur_day or p.date in open_periods)):
            nd = f"the {short_date(nxt.date)} check" if nxt else "the next check"
            st.markdown(f"**Pays the bills due {short_date(p.date)} through {short_date(p.window_end)}.** "
                        f"Anything due on or after {md(p.window_end + timedelta(days=1))} waits for {nd}.")
            for orig, reason in p.moved_from:
                st.info(f"Deposit moved earlier: {orig:%b} {orig.day} is {reason}.")

            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Carried in", money(p.carried_in))
            m2.metric("+ Paycheck", money(p.income - p.keep),
                      " + ".join(f"{i['name']} {money(i['amount'], False)}" for i in p.incomes), delta_color="off")
            m3.metric("− Bills due", money(p.bills_total))
            m4.metric("= Carry to next check", money(p.carried_out))

            if p.bills:
                df = pd.DataFrame({
                    "Paid": [bool(state["paid"].get(b.key)) for b in p.bills],
                    "Bill": [b.name for b in p.bills],
                    "Due": [short_date(b.due) for b in p.bills],
                    "When": [days_after(b, p.date) for b in p.bills],
                    "Amount": [b.amount for b in p.bills],
                    "Note": [note(b) for b in p.bills],
                })
                ekey = f"sched_{p.date.isoformat()}_{st.session_state.rev}"
                st.data_editor(
                    df, key=ekey, hide_index=True, use_container_width=True,
                    disabled=["Bill", "Due", "When", "Note"],
                    on_change=apply_schedule_edits, args=(ekey, p.bills, p.date),
                    column_config={
                        "Paid": st.column_config.CheckboxColumn(width="small"),
                        "Amount": st.column_config.NumberColumn(min_value=0.0, step=0.01, format="$%.2f",
                                                                help="Change this payment only"),
                    },
                )
            else:
                st.caption("No bills come due before the next check.")

            if p.carried_out < 0:
                st.warning(f"Short by {money(-p.carried_out)}: these bills cost more than this paycheck plus "
                           "what was carried in.")
            elif p.left < 0:
                st.caption(f"This paycheck alone is {money(-p.left)} short. The {money(p.carried_in)} carried in "
                           "from earlier checks covers it.")

            if nxt and nxt.bills:
                waiting = ", ".join(f"{b.name} ({md(b.due)})" for b in nxt.bills if b.amount > 0)
                st.caption(f"**Saved for the {short_date(nxt.date)} check:** {money(nxt.bills_total)} due "
                           f"{md(nxt.date)}–{md(nxt.window_end)}: {waiting}")

# ---------------------------------------------------------------- bills editor

with tab_bills:
    st.caption("Edit names, due days and amounts here to change a bill for **every month** (payments where you typed "
               "a one-time amount on the Pay schedule keep that amount). Changes save as soon as you leave the cell. "
               "Add rows at the bottom, or select rows and delete them.")
    bills_df = pd.DataFrame([{
        "id": b["id"], "Bill": b["name"], "Due day": int(b["day"]), "Amount": float(b["amount"]),
        "Include": b.get("active", True) is not False,
    } for b in state["bills"]], columns=["id", "Bill", "Due day", "Amount", "Include"])
    edited = st.data_editor(
        bills_df, key=f"bills_{st.session_state.rev}", num_rows="dynamic", hide_index=True, use_container_width=True,
        column_config={
            "id": None,
            "Bill": st.column_config.TextColumn(required=True),
            "Due day": st.column_config.NumberColumn(min_value=1, max_value=31, step=1, required=True),
            "Amount": st.column_config.NumberColumn(min_value=0.0, step=0.01, format="$%.2f"),
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
### The rule
**A paycheck pays only the bills that come due from its payday up to the day before the next payday.**
Bills due later are saved for the check they fall under. For example, the Nov 27 check pays everything due
Nov 27 through Dec 10, including the Dec 1 mortgage. The Pontoon payment due Dec 15 waits for the Dec 11 check.

**Leftover money carries forward.** Each check starts with what the earlier checks didn't spend
("Carried in"), adds the paycheck, and pays its bills. What's left is carried to the next check.

### Why some checks come up short
Paydays are every 14 days but bills are monthly. The check that covers the 1st of the month carries the mortgage
and the other early-month bills (about $3,880 through the 8th, plus Hyundai on the 28th when it falls in that
window), so that check alone is a few hundred dollars short. The check before it has $2,100–$3,100 left, and that
money carries over and covers it. The chart shows whether the money carried forward ever drops below zero.

### Notes on the spreadsheet
- Ten bills are $0 (Raymond, Express, both CareCredit accounts, Crossroads School, both Misc., Farm Bureau
  Life, NBA League Pass). If any are real, type the amounts on the Bills tab.
- The second paycheck was $1,505 on Sep 4 and $1,501 after that. The app uses $1,501.
""")


