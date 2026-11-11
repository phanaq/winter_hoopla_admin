"""Winter Hoopla organizer tool: payments, attendance and refunds (phone-first).

Run locally:   streamlit run admin.py --server.port 8502
Deploy:        second Streamlit Community Cloud app, main file = admin.py (see ADMIN_TOOL.md)
Pure logic lives in admin_logic.py.
"""
import hmac
import html
from datetime import datetime
from typing import Dict, List

import pandas as pd
import streamlit as st

import admin_logic as L

TABLE = "signups"
SIDE = {"mmp": "MMP", "wmp": "WMP"}
VENMO_HANDLE = "aphan"

st.set_page_config(page_title="Hoopla Admin", page_icon="🥏", layout="centered")
st.markdown(
    """<style>
    .block-container {padding-top: 1.2rem; padding-left: .8rem; padding-right: .8rem;}
    div.stButton > button {min-height: 2.8rem;}
    [data-testid="stMetricValue"] {font-size: 1.5rem;}
    /* keep 2-3 columns side by side on phones instead of stacking */
    div[data-testid="stHorizontalBlock"] {flex-wrap: nowrap !important; gap: .5rem !important;}
    div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"] {min-width: 0 !important;}
    </style>""",
    unsafe_allow_html=True,
)


def esc(text) -> str:
    """Escape player-entered text (and $, which Streamlit markdown treats as LaTeX) for st.markdown."""
    return html.escape(str(text or "")).replace("$", "\\$")


# ---------------------------------------------------------------------------
# Password gate — the app never runs without [admin] password configured
# ---------------------------------------------------------------------------
def require_password() -> None:
    try:
        expected = str(st.secrets["admin"]["password"])
    except Exception:
        expected = ""
    if not expected:
        st.error("Admin password is not configured. Add an [admin] section with password = \"...\" to the app's secrets.")
        st.stop()
    if st.session_state.get("authed"):
        return
    st.title("🥏 Hoopla Admin")
    with st.form("login"):
        pw = st.text_input("Password", type="password")
        ok = st.form_submit_button("Log in", type="primary", use_container_width=True)
    if ok:
        if hmac.compare_digest(pw.encode(), expected.encode()):
            st.session_state.authed = True
            st.rerun()
        st.error("Wrong password.")
    st.stop()


require_password()


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
@st.cache_resource
def get_db():
    from supabase import create_client

    try:
        url, key = st.secrets["supabase"]["url"], st.secrets["supabase"]["key"]
    except Exception:
        st.error("Supabase is not configured. Add [supabase] url and key to the app's secrets.")
        st.stop()
    return create_client(url, key)


@st.cache_data(ttl=300, show_spinner=False)
def has_payment_columns() -> bool:
    """False until supabase/migrations/20261111020000_payment_tracking.sql has been run."""
    try:
        get_db().table(TABLE).select("id," + ",".join(L.PAYMENT_COLUMNS)).limit(1).execute()
        return True
    except Exception:
        return False


def load_rows() -> List[Dict]:
    return get_db().table(TABLE).select("*").order("id").execute().data or []


def write(row_id: int, values: Dict) -> None:
    """Update one row immediately (used from button callbacks, so the page reruns right after)."""
    if not values:
        return
    try:
        get_db().table(TABLE).update(values).eq("id", row_id).execute()
        st.session_state.flash = ("success", "Saved")
    except Exception as e:  # show, don't crash the field screen
        st.session_state.flash = ("error", f"Couldn't save: {e}")


HAS_COLS = has_payment_columns()
rows = load_rows()

if not HAS_COLS:
    st.warning(
        "**Run the migration** `supabase/migrations/20261111020000_payment_tracking.sql` in the Supabase "
        "SQL Editor to enable attendance, amounts, notes, refunds and imports. Until then only "
        "'paid' (paid_verified) can be recorded.",
        icon="⚠️",
    )
    st.button("I ran it — recheck", on_click=has_payment_columns.clear)

flash = st.session_state.pop("flash", None)
if flash:
    (st.toast if flash[0] == "success" else st.error)(flash[1])

today_session = L.default_session(datetime.now(L.TZ))
session = st.selectbox("Week", L.SESSIONS, index=L.SESSIONS.index(today_session),
                       format_func=L.fmt_session, key="session", label_visibility="collapsed")
page = st.segmented_control("Page", ["At the field", "Refunds", "Season", "Import", "Export"],
                            default="At the field", key="page", label_visibility="collapsed") or "At the field"


# ---------------------------------------------------------------------------
# At the field
# ---------------------------------------------------------------------------
def default_via(row: Dict) -> str:
    return row.get("paid_via") or L.METHOD_TO_VIA.get(row.get("payment_method") or "", "cash")


def toggle_panel(name: str) -> None:
    """Only one card's detail panel is open at a time; tracked by row id, so it can't jump
    to another player when the list re-sorts (st.popover has no key, so it would)."""
    st.session_state.panel = None if st.session_state.get("panel") == name else name


def write_and_close(row_id: int, values: Dict) -> None:
    write(row_id, values)
    st.session_state.panel = None


def player_card(row: Dict, waitlisted: bool = False) -> None:
    rid = row["id"]
    with st.container(border=True):
        here = row.get("attended")
        mark = " · ✋ here" if here is True else (" · 🚫 no-show" if here is False else "")
        st.markdown(f"**{esc(row['name'])}** · {SIDE.get(row['gender_match'], '')}{mark}  \n"
                    f"{esc('⏸️ Waitlisted — nothing owed' if waitlisted and not L.is_paid(row) else L.status_badge(row))}"
                    + (f"  \n<small>📝 {esc(row['admin_notes'])}</small>" if row.get("admin_notes") else ""),
                    unsafe_allow_html=True)
        a, b = st.columns(2)
        if HAS_COLS:
            if here is True:
                a.button("Undo here", key=f"here{rid}", use_container_width=True,
                         on_click=write, args=(rid, L.attendance_update(None)))
            else:
                a.button("✋ Here", key=f"here{rid}", type="primary", use_container_width=True,
                         on_click=write, args=(rid, L.attendance_update(True)))
        is_open = st.session_state.get("panel") == f"p{rid}"
        label = ("✅ Paid · more" if L.is_paid(row) else "💰 Mark paid") + (" ▴" if is_open else "")
        b.button(label, key=f"open{rid}", use_container_width=True, on_click=toggle_panel, args=(f"p{rid}",))
        if not is_open:
            return
        if L.is_paid(row):
            st.button("Undo payment", key=f"unpay{rid}", use_container_width=True,
                      on_click=write_and_close, args=(rid, L.clear_payment_update(HAS_COLS)))
        else:
            if HAS_COLS:
                st.segmented_control("Via", L.PAID_VIAS, default=default_via(row), key=f"via{rid}")
                st.number_input("Amount $", min_value=0.0, value=L.PRICE, step=5.0, key=f"amt{rid}")
            st.button(f"Record payment for {row['name']}", key=f"pay{rid}", type="primary",
                      use_container_width=True,
                      on_click=lambda: write_and_close(rid, L.payment_update(
                          st.session_state.get(f"via{rid}") or default_via(row),
                          st.session_state.get(f"amt{rid}", L.PRICE), has_cols=HAS_COLS)))
        if HAS_COLS:
            st.text_input("Note", value=row.get("admin_notes") or "", key=f"note{rid}")
            n1, n2 = st.columns(2)
            n1.button("Save note", key=f"savenote{rid}", use_container_width=True,
                      on_click=lambda: write(rid, {"admin_notes": st.session_state[f"note{rid}"].strip() or None}))
            if here is False:
                n2.button("Clear no-show", key=f"noshow{rid}", use_container_width=True,
                          on_click=write, args=(rid, L.attendance_update(None)))
            else:
                n2.button("🚫 No-show", key=f"noshow{rid}", use_container_width=True,
                          on_click=write, args=(rid, L.attendance_update(False)))


def field_page() -> None:
    w = L.week_summary(rows, session)
    roster, waitlist = w["roster"], w["waitlist"]
    here = f"✋ **{w['here']}**/{len(roster)} here · " if HAS_COLS else ""
    st.markdown(f"{here}💰 **\\${w['collected_all']:g}** in · "
                f"⏳ **\\${w['outstanding_amount']:g}** owed ({w['outstanding']})")
    if not roster and not waitlist:
        st.info("No signups for this week yet.")
        return
    f1, f2 = st.columns([6, 1])
    flt = f1.segmented_control("Show", ["Owes me", "Not here", "All"] if HAS_COLS else ["Owes me", "All"], default="All",
                               key="filter", label_visibility="collapsed") or "All"
    f2.button("↻", help="Refresh (pick up taps from other devices)", key="refresh")
    people = sorted(roster, key=lambda r: (not L.owes(r), r["name"].lower()))
    if flt == "Owes me":
        people = [r for r in people if L.owes(r)]
    elif flt == "Not here":
        people = [r for r in people if r.get("attended") is not True]
    st.caption(f"{len(people)} of {len(roster)} rostered · owes first")
    for r in people:
        player_card(r)
    with st.expander(f"Waitlist ({len(waitlist)}) — no payment owed unless pulled in"):
        for r in waitlist:
            player_card(r, waitlisted=True)


# ---------------------------------------------------------------------------
# Refunds
# ---------------------------------------------------------------------------
def refunds_page() -> None:
    refunds = L.refund_rows(rows)
    t = L.refund_totals(refunds)
    a, b = st.columns(2)
    a.metric("Refunds owed", f"${t['owed']:g}")
    b.metric("Refunded", f"${t['refunded']:g}")
    st.caption("Suggestions come from cancellations: rostered + paid/said Venmo or Zelle → owed; "
               "waitlisted, in-person or nothing declared → not needed. Tap to override.")
    if not refunds:
        st.info("No cancellations yet.")
        return
    groups = {"owed": "Owed", "refunded": "Refunded", "not_needed": "Not needed"}
    for status, label in groups.items():
        items = [r for r in refunds if r["effective_refund_status"] == status]
        if not items:
            continue
        with st.expander(f"{label} ({len(items)})", expanded=status == "owed"):
            for r in items:
                refund_card(r)


def refund_card(r: Dict) -> None:
    rid = r["id"]
    with st.container(border=True):
        suggested = "" if r.get("refund_status") else " (suggested)"
        st.markdown(f"**{esc(r['name'])}** · {L.fmt_session(pd.Timestamp(r['session_date']).date())}  \n"
                    f"{r['effective_refund_status'].replace('_', ' ')}{suggested} · "
                    f"\\${r['effective_refund_amount']:g}  \n<small>{esc(r['refund_reason'])}</small>",
                    unsafe_allow_html=True)
        if not HAS_COLS:
            return
        is_open = st.session_state.get("panel") == f"r{rid}"
        st.button("Update refund" + (" ▴" if is_open else ""), key=f"ropen{rid}", use_container_width=True,
                  on_click=toggle_panel, args=(f"r{rid}",))
        if not is_open:
            return
        amt = st.number_input("Amount $", min_value=0.0, value=float(r["effective_refund_amount"] or L.PRICE),
                              step=5.0, key=f"ramt{rid}")
        when = st.date_input("Refunded on", value=datetime.now(L.TZ).date(), key=f"rdate{rid}")
        st.button(f"💸 Mark {r['name']} refunded", key=f"rdone{rid}", type="primary", use_container_width=True,
                  on_click=write_and_close, args=(rid, {
                      "refund_status": "refunded", "refund_amount": amt,
                      "refunded_at": datetime.combine(when, datetime.min.time(), L.TZ).isoformat()}))
        c1, c2 = st.columns(2)
        c1.button("Owed", key=f"rowed{rid}", use_container_width=True, on_click=write_and_close,
                  args=(rid, {"refund_status": "owed", "refund_amount": amt, "refunded_at": None}))
        c2.button("Not needed", key=f"rnn{rid}", use_container_width=True, on_click=write_and_close,
                  args=(rid, {"refund_status": "not_needed", "refund_amount": None, "refunded_at": None}))


# ---------------------------------------------------------------------------
# Season overview
# ---------------------------------------------------------------------------
def season_page() -> None:
    df = L.season_overview(rows)
    if df.empty:
        st.info("No signups yet.")
        return
    t = L.refund_totals(L.refund_rows(rows))
    a, b, c = st.columns(3)
    a.metric("Collected", f"${df['collected'].sum():g}")
    b.metric("Unpaid", f"${df['owed'].sum():g}")
    c.metric("Refunds owed", f"${t['owed']:g}")
    cols = {"name": "Name", "rostered_weeks": "Rostered", "showed_up": "Showed", "no_shows": "No-show",
            "waitlisted_weeks": "Waitlist", "cancelled": "Cancelled", "weeks_paid": "Paid wks",
            "collected": "Paid $", "owed": "Owes $", "refund_owed": "Refund owed $", "refunded": "Refunded $",
            "weeks": "Weeks", "email": "Email"}
    st.dataframe(df[list(cols)].rename(columns=cols), hide_index=True, use_container_width=True,
                 column_config={"Name": st.column_config.TextColumn(pinned=True)})
    if not HAS_COLS:
        st.caption("Attendance columns appear after the migration is run.")


# ---------------------------------------------------------------------------
# Import (Venmo statement / generic Zelle-bank CSV)
# ---------------------------------------------------------------------------
def import_page() -> None:
    if not HAS_COLS:
        st.info("Imports need the migration (payment_ref column) — run it first.")
        return
    st.caption(f"Matches incoming payments to unpaid rostered players for **{L.fmt_session(session)}** "
               "(change the week above). Nothing is saved until you confirm.")
    kind = st.radio("File type", ["Venmo statement", "Zelle / bank CSV"], horizontal=True)
    up = st.file_uploader("CSV file", type=["csv"])
    if not up:
        return
    try:
        data = up.getvalue()
        payments = L.parse_venmo_csv(data, VENMO_HANDLE) if kind == "Venmo statement" \
            else L.parse_generic_csv(data, "zelle")
    except Exception as e:
        st.error(f"Couldn't read that file: {e}")
        return
    used = {r.get("payment_ref") for r in rows if r.get("payment_ref")}
    proposals = L.match_payments(payments, rows, session, used_refs=used)
    st.write(f"{len(payments)} incoming payments in file · {len(proposals)} plausibly for this week "
             f"(by date window or a date in the note).")
    if not proposals:
        return
    roster, _ = L.build_roster(L.active_rows(rows, session))
    choices = {0: "— skip —"}
    choices.update({r["id"]: r["name"] for r in roster if not L.is_paid(r)})
    with st.form("import"):
        picks = []
        for i, m in enumerate(proposals):
            p = m["payment"]
            when = p.when.astimezone(L.TZ).strftime("%b %-d") if p.when else "?"
            warn = "" if m["amount_ok"] else f" ⚠️ amount ≠ \\${L.PRICE:g}"
            st.markdown(f"**{esc(p.name)}** · \\${p.amount:g} · {when}{warn}  \n<small>{esc(p.note)}</small>",
                        unsafe_allow_html=True)
            default = m["row"]["id"] if m["row"] else 0
            rid = st.selectbox(f"Match ({m['score']:.0%} name match)" if m["row"] else "Match (no confident match)",
                               list(choices), index=list(choices).index(default) if default in choices else 0,
                               format_func=choices.get, key=f"imp{i}")
            picks.append((rid, p))
            st.divider()
        go = st.form_submit_button("Record selected payments", type="primary", use_container_width=True)
    if go:
        chosen = [(rid, p) for rid, p in picks if rid]
        if len({rid for rid, _ in chosen}) != len(chosen):
            st.error("The same player is selected for two payments — fix that first.")
            return
        for rid, p in chosen:
            get_db().table(TABLE).update(L.payment_update(p.source, p.amount, p.when, p.ref)).eq("id", rid).execute()
        st.session_state.flash = ("success", f"Recorded {len(chosen)} payments")
        st.rerun()


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
def export_page() -> None:
    w = L.week_summary(rows, session)
    week = [dict(r, list="roster", status=L.payment_status(r)) for r in w["roster"]] + \
           [dict(r, list="waitlist", status="Waitlisted") for r in w["waitlist"]]
    st.download_button(f"⬇️ {L.fmt_session(session)} CSV", pd.DataFrame(week).to_csv(index=False),
                       f"hoopla_{session}.csv", "text/csv", use_container_width=True, disabled=not week)
    st.download_button("⬇️ Whole season (all rows) CSV", pd.DataFrame(rows).to_csv(index=False),
                       "hoopla_season.csv", "text/csv", use_container_width=True, disabled=not rows)
    st.download_button("⬇️ Season overview CSV", L.season_overview(rows).to_csv(index=False),
                       "hoopla_overview.csv", "text/csv", use_container_width=True, disabled=not rows)


{"At the field": field_page, "Refunds": refunds_page, "Season": season_page,
 "Import": import_page, "Export": export_page}[page]()
