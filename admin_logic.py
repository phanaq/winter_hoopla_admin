"""Pure logic for the Winter Hoopla admin tool (no Streamlit, no database).

Everything here works on plain dicts shaped like rows of public.signups, so it
can be tested with fabricated data.
"""
import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from difflib import SequenceMatcher
from typing import Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

import pandas as pd

TZ = ZoneInfo("America/Chicago")
FIRST_SESSION = date(2026, 11, 16)  # Mondays
NUM_SESSIONS = 7
SESSIONS = [FIRST_SESSION + timedelta(weeks=i) for i in range(NUM_SESSIONS)]
END_TIME = time(22, 15)
PRICE = 20.0
MAX_PLAYERS_PER_SIDE = 10

PAID_VIAS = ["venmo", "zelle", "cash"]
REFUND_STATUSES = ["owed", "refunded", "not_needed"]
METHOD_TO_VIA = {"venmo": "venmo", "zelle": "zelle", "in_person": "cash"}

STATUS_PAID = "Paid (verified)"
STATUS_VERIFY = "Says paid on {} — verify"
STATUS_IN_PERSON = "Pay in person — owed at field"
STATUS_UNDECLARED = "No payment declared (moved up from waitlist)"

# Columns added by supabase/migrations/20261111020000_payment_tracking.sql
PAYMENT_COLUMNS = [
    "amount_paid", "paid_via", "paid_at", "payment_ref",
    "refund_status", "refund_amount", "refunded_at", "admin_notes",
    "attended", "attended_marked_at",
]


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------
def parse_ts(value) -> Optional[datetime]:
    """Parse a timestamptz string from PostgREST (any fractional precision) to aware UTC."""
    if value is None or value == "" or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=TZ)
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize(TZ)
    return ts.to_pydatetime()


def session_ends_at(session: date) -> datetime:
    return datetime.combine(session, END_TIME, TZ)


def default_session(at: datetime) -> date:
    """The next session that hasn't finished yet (last session once the season is over)."""
    for s in SESSIONS:
        if at < session_ends_at(s):
            return s
    return SESSIONS[-1]


def fmt_session(d: date) -> str:
    return f"Week {SESSIONS.index(d) + 1} · {d.strftime('%a %b')} {d.day}" if d in SESSIONS else d.isoformat()


# ---------------------------------------------------------------------------
# Roster
# ---------------------------------------------------------------------------
def build_roster(rows: Iterable[Dict]) -> Tuple[List[Dict], List[Dict]]:
    """Same rule as app.py: in id order, each gender side takes the first 10; the rest wait."""
    counts = {"mmp": 0, "wmp": 0}
    roster, waitlist = [], []
    for row in sorted(rows, key=lambda r: r["id"]):
        side = row["gender_match"]
        if counts.get(side, 0) < MAX_PLAYERS_PER_SIDE:
            counts[side] = counts.get(side, 0) + 1
            roster.append(row)
        else:
            waitlist.append(row)
    return roster, waitlist


def active_rows(rows: Iterable[Dict], session: date) -> List[Dict]:
    s = session.isoformat()
    return [r for r in rows if str(r["session_date"]) == s and not r.get("cancelled_at")]


def was_rostered_when_cancelled(row: Dict, all_rows: Iterable[Dict]) -> bool:
    """Rebuild the roster as it stood just before `row` was cancelled.

    A row counts as active at that moment if it was created before the cancel
    time and was not itself cancelled at or before it.
    """
    cancelled = parse_ts(row.get("cancelled_at"))
    if cancelled is None:
        return False
    snapshot = []
    for r in all_rows:
        if str(r["session_date"]) != str(row["session_date"]):
            continue
        created = parse_ts(r.get("created_at"))
        r_cancel = parse_ts(r.get("cancelled_at"))
        if r["id"] == row["id"] or (
            (created is None or created < cancelled) and (r_cancel is None or r_cancel > cancelled)
        ):
            snapshot.append(r)
    roster, _ = build_roster(snapshot)
    return any(r["id"] == row["id"] for r in roster)


# ---------------------------------------------------------------------------
# Payment status
# ---------------------------------------------------------------------------
def is_paid(row: Dict) -> bool:
    return bool(row.get("paid_verified")) or (_num(row.get("amount_paid")) or 0) > 0


def amount_collected(row: Dict) -> float:
    """What we actually received: amount_paid, or the nightly price if only paid_verified is set."""
    amt = _num(row.get("amount_paid"))
    if amt is not None and amt > 0:
        return amt
    return PRICE if row.get("paid_verified") else 0.0


def payment_status(row: Dict) -> str:
    if is_paid(row):
        return STATUS_PAID
    method = row.get("payment_method")
    if method in ("venmo", "zelle"):
        return STATUS_VERIFY.format("Venmo" if method == "venmo" else "Zelle")
    if method == "in_person":
        return STATUS_IN_PERSON
    return STATUS_UNDECLARED


def status_badge(row: Dict) -> str:
    """Short phone-friendly status label."""
    if is_paid(row):
        via = row.get("paid_via")
        return f"✅ Paid ${amount_collected(row):g}" + (f" ({via})" if via else "")
    method = row.get("payment_method")
    if method in ("venmo", "zelle"):
        return f"❓ Says {method.title()} — verify"
    if method == "in_person":
        return f"💵 Owes ${PRICE:g} (in person)"
    return "⚠️ Nothing declared"


def owes(row: Dict) -> bool:
    return not is_paid(row)


def week_summary(rows: List[Dict], session: date) -> Dict:
    roster, waitlist = build_roster(active_rows(rows, session))
    unpaid = [r for r in roster if not is_paid(r)]
    return {
        "roster": roster,
        "waitlist": waitlist,
        "expected": PRICE * len(roster),
        "collected": sum(amount_collected(r) for r in roster),
        "outstanding": len(unpaid),
        "outstanding_amount": PRICE * len(unpaid),
        "here": sum(1 for r in roster + waitlist if r.get("attended") is True),
        "collected_all": sum(amount_collected(r) for r in roster + waitlist),
    }


# ---------------------------------------------------------------------------
# Refunds
# ---------------------------------------------------------------------------
def suggest_refund(row: Dict, all_rows: List[Dict]) -> Tuple[str, str]:
    """Return (suggested refund_status, reason) for a cancelled row."""
    if is_paid(row):
        return "owed", "Payment was recorded"
    rostered = was_rostered_when_cancelled(row, all_rows)
    method = row.get("payment_method")
    if rostered and method in ("venmo", "zelle"):
        return "owed", f"Rostered and said they paid on {method.title()} (verify you received it)"
    if not rostered:
        return "not_needed", "Was on the waitlist when they cancelled"
    if method == "in_person":
        return "not_needed", "Was going to pay in person"
    return "not_needed", "Never declared or recorded a payment"


def refund_rows(all_rows: List[Dict]) -> List[Dict]:
    """Cancelled rows annotated with suggested and effective refund status/amount."""
    out = []
    for r in all_rows:
        if not r.get("cancelled_at"):
            continue
        suggestion, reason = suggest_refund(r, all_rows)
        status = r.get("refund_status") or suggestion
        amount = _num(r.get("refund_amount"))
        if amount is None:
            amount = (amount_collected(r) or PRICE) if status != "not_needed" else 0.0
        out.append(dict(r, suggested_refund=suggestion, refund_reason=reason,
                        effective_refund_status=status, effective_refund_amount=amount))
    return sorted(out, key=lambda r: (str(r["session_date"]), r["id"]))


def refund_totals(refunds: List[Dict]) -> Dict[str, float]:
    owed = sum(r["effective_refund_amount"] for r in refunds if r["effective_refund_status"] == "owed")
    done = sum(r["effective_refund_amount"] for r in refunds if r["effective_refund_status"] == "refunded")
    return {"owed": owed, "refunded": done}


# ---------------------------------------------------------------------------
# Season overview
# ---------------------------------------------------------------------------
def season_overview(all_rows: List[Dict]) -> pd.DataFrame:
    rostered_ids, waitlisted_ids = set(), set()
    for s in SESSIONS:
        roster, waitlist = build_roster(active_rows(all_rows, s))
        rostered_ids.update(r["id"] for r in roster)
        waitlisted_ids.update(r["id"] for r in waitlist)
    refunds = {r["id"]: r for r in refund_rows(all_rows)}

    players: Dict[str, Dict] = {}
    for r in sorted(all_rows, key=lambda r: r["id"]):
        p = players.setdefault(r["email"].lower(), {
            "name": r["name"], "email": r["email"].lower(), "rostered_weeks": 0, "waitlisted_weeks": 0,
            "cancelled": 0, "showed_up": 0, "no_shows": 0, "weeks_paid": 0, "collected": 0.0, "owed": 0.0,
            "refund_owed": 0.0, "refunded": 0.0, "weeks": [],
        })
        p["name"] = r["name"]  # latest spelling
        if r["id"] in rostered_ids:
            p["rostered_weeks"] += 1
            p["weeks"].append(str(r["session_date"])[5:])
            if not is_paid(r):
                p["owed"] += PRICE
        elif r["id"] in waitlisted_ids:
            p["waitlisted_weeks"] += 1
        if r.get("attended") is True:
            p["showed_up"] += 1
        elif r.get("attended") is False and r["id"] in rostered_ids:
            p["no_shows"] += 1
        if r.get("cancelled_at"):
            p["cancelled"] += 1
            ref = refunds[r["id"]]
            if ref["effective_refund_status"] == "owed":
                p["refund_owed"] += ref["effective_refund_amount"]
            elif ref["effective_refund_status"] == "refunded":
                p["refunded"] += ref["effective_refund_amount"]
        if is_paid(r):
            p["weeks_paid"] += 1
            p["collected"] += amount_collected(r)
    df = pd.DataFrame(list(players.values()))
    if df.empty:
        return df
    df["weeks"] = df["weeks"].apply(", ".join)
    return df.sort_values("name", key=lambda s: s.str.lower()).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Payment imports (Venmo statement CSV / generic CSV)
# ---------------------------------------------------------------------------
@dataclass
class Payment:
    ref: str  # unique id from the statement, stored in signups.payment_ref to avoid double imports
    when: Optional[datetime]
    name: str
    amount: float
    note: str = ""
    source: str = "venmo"
    session_hint: Optional[date] = None  # play date mentioned in the note
    extra: Dict = field(default_factory=dict)


def _num(value) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return None if pd.isna(value) else float(value)
    s = str(value).strip()
    if not s:
        return None
    neg = s.startswith("-") or s.startswith("(")
    digits = re.sub(r"[^0-9.]", "", s)
    if not digits:
        return None
    try:
        v = float(digits)
    except ValueError:
        return None
    return -v if neg else v


def _read_rows(data) -> List[List[str]]:
    if isinstance(data, bytes):
        data = data.decode("utf-8-sig", errors="replace")
    return list(csv.reader(io.StringIO(data)))


def _find_header(rows: List[List[str]], required: Iterable[str]) -> int:
    req = [x.lower() for x in required]
    for i, row in enumerate(rows):
        cells = [c.strip().lower() for c in row]
        if all(any(r in c for c in cells) for r in req):
            return i
    raise ValueError(f"Couldn't find a header row with columns: {', '.join(required)}")


def _col(header: List[str], *names: str) -> Optional[int]:
    cells = [c.strip().lower() for c in header]
    for n in names:  # exact match first, then prefix
        if n.lower() in cells:
            return cells.index(n.lower())
    for n in names:
        for i, c in enumerate(cells):
            if c.startswith(n.lower()):
                return i
    for n in names:  # then substring ("Posting Date"), only for distinctive names
        for i, c in enumerate(cells):
            if len(n) >= 4 and n.lower() in c:
                return i
    return None


_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def session_from_note(note: str) -> Optional[date]:
    """Find a season play date mentioned in a payment note ("11/23", "Nov 23", "week 2")."""
    text = (note or "").lower()
    candidates = []
    for m, d in re.findall(r"\b(1[0-2]|0?[1-9])[/-]([0-3]?\d)\b", text):
        candidates.append((int(m), int(d)))
    for mon, d in re.findall(r"\b(nov|dec)[a-z]*\.?\s*([0-3]?\d)(?:st|nd|rd|th)?\b", text):
        candidates.append((_MONTHS[mon], int(d)))
    for m, d in candidates:
        for s in SESSIONS:
            if (s.month, s.day) == (m, d):
                return s
    wk = re.search(r"\b(?:week|wk)\s*#?\s*([1-7])\b", text)
    if wk:
        return SESSIONS[int(wk.group(1)) - 1]
    return None


def parse_venmo_csv(data, my_handle: str = "") -> List[Payment]:
    """Parse Venmo's downloadable statement CSV and return incoming, completed payments.

    The statement has a few title rows before the real header (", ID, Datetime,
    Type, Status, Note, From, To, Amount (total), ...") and summary rows after;
    both are skipped. Incoming money is a positive "Amount (total)" (e.g. "+ $20.00").
    """
    rows = _read_rows(data)
    h = _find_header(rows, ["datetime", "amount"])
    header = rows[h]
    c = {k: _col(header, *names) for k, names in {
        "id": ["id"], "dt": ["datetime", "date"], "type": ["type"], "status": ["status"],
        "note": ["note"], "from": ["from"], "to": ["to"], "amount": ["amount (total)", "amount"],
    }.items()}
    me = my_handle.lstrip("@").lower()
    out = []
    for row in rows[h + 1:]:
        get = lambda k: row[c[k]].strip() if c[k] is not None and c[k] < len(row) else ""
        amount = _num(get("amount"))
        when_raw = get("dt")
        if amount is None or amount <= 0 or not when_raw:
            continue
        status = get("status").lower()
        if status and not status.startswith(("complete", "issued")):
            continue
        typ = get("type").lower()
        if typ and not any(t in typ for t in ("payment", "charge")):
            continue  # skip transfers, refunds etc.
        # For a payment they sent us, the payer is "From"; for a charge we requested, it's "To".
        payer = get("from") if "charge" not in typ else get("to")
        if me and payer.lstrip("@").lower() == me:
            payer = get("to") if payer == get("from") else get("from")
        try:
            when = parse_ts(when_raw)
        except (ValueError, TypeError):
            when = None
        note = get("note")
        out.append(Payment(ref=f"venmo:{get('id') or when_raw + payer}", when=when, name=payer,
                           amount=amount, note=note, source="venmo", session_hint=session_from_note(note)))
    return out


_ZELLE_NAME = re.compile(r"(?:zelle\s+)?(?:payment\s+)?from\s+(.+?)(?:\s+(?:on|conf|ref|id|memo)\b|[;:#]|$)", re.I)


def parse_generic_csv(data, source: str = "zelle") -> List[Payment]:
    """Parse a simple bank/Zelle export with date, name, amount, memo/description columns.

    If there's no name column, the payer is pulled from a description like
    "Zelle payment from JANE DOE Conf# abc123". Only positive amounts are kept.
    """
    rows = _read_rows(data)
    h = _find_header(rows, ["date", "amount"])
    header = rows[h]
    c = {
        "date": _col(header, "date", "posting date", "transaction date"),
        "name": _col(header, "name", "from", "sender", "payer"),
        "amount": _col(header, "amount", "credit"),
        "memo": _col(header, "memo", "description", "note", "details"),
        "ref": _col(header, "reference", "ref", "id", "confirmation"),
    }
    out = []
    for row in rows[h + 1:]:
        get = lambda k: row[c[k]].strip() if c[k] is not None and c[k] < len(row) else ""
        amount = _num(get("amount"))
        if amount is None or amount <= 0:
            continue
        memo, name = get("memo"), get("name")
        if not name:
            m = _ZELLE_NAME.search(memo)
            name = m.group(1).strip() if m else ""
        try:
            when = parse_ts(get("date")) if get("date") else None
        except (ValueError, TypeError):
            when = None
        conf = re.search(r"conf(?:irmation)?\s*#?\s*:?\s*([a-z0-9]+)", memo, re.I)
        ref = get("ref") or (conf.group(1) if conf else f"{get('date')}|{name}|{amount}")
        out.append(Payment(ref=f"{source}:{ref}", when=when, name=name.title() if name.isupper() else name,
                           amount=amount, note=memo, source=source, session_hint=session_from_note(memo)))
    return out


def _norm(name: str) -> List[str]:
    return re.sub(r"[^a-z ]", " ", (name or "").lower()).split()


def name_score(a: str, b: str) -> float:
    """0..1 similarity between a statement name and a signup name.

    Uses the best of: whole-string similarity, and first-name match with
    last name / last-initial agreement (so "Jane Doe" ~ "Jane D." ~ "jane").
    """
    ta, tb = _norm(a), _norm(b)
    if not ta or not tb:
        return 0.0
    whole = SequenceMatcher(None, " ".join(ta), " ".join(tb)).ratio()
    first = SequenceMatcher(None, ta[0], tb[0]).ratio()
    token = 0.0
    if first >= 0.85:
        if len(ta) > 1 and len(tb) > 1:
            la, lb = ta[-1], tb[-1]
            if la == lb:
                token = 1.0
            elif la[0] == lb[0] and (len(la) == 1 or len(lb) == 1):
                token = 0.9  # last initial only
            else:
                token = 0.5 * first
        else:
            token = 0.8 * first  # only a first name on one side
    return max(whole, token)


def payment_in_window(p: Payment, session: date) -> bool:
    """With no date in the note, a payment plausibly belongs to a session if it was made from
    the Monday before (signups open the Friday before) through the Thursday after."""
    if p.when is None:
        return True
    d = p.when.astimezone(TZ).date()
    return session - timedelta(days=7) <= d <= session + timedelta(days=3)


def match_payments(payments: List[Payment], rows: List[Dict], session: date,
                   threshold: float = 0.6, used_refs: Iterable[str] = ()) -> List[Dict]:
    """Propose payment -> rostered-player matches for one session.

    Candidates: unpaid rostered players. A payment is considered when its note names
    this session, or it names no session and was made within the session's window.
    Each payment and each player is used at most once (best scores win).
    Returns one dict per considered payment with the proposed row (or None).
    """
    used = set(used_refs)
    roster, _ = build_roster(active_rows(rows, session))
    targets = [r for r in roster if not is_paid(r)]
    pays = [p for p in payments if p.ref not in used and (
        p.session_hint == session or (p.session_hint is None and payment_in_window(p, session)))]

    scored = []
    for pi, p in enumerate(pays):
        for r in targets:
            s = max(name_score(p.name, r["name"]), name_score(p.name, r["email"].split("@")[0].replace(".", " ")))
            if p.session_hint == session:
                s = min(1.0, s + 0.05)
            if s >= threshold:
                scored.append((s, pi, r["id"]))
    scored.sort(key=lambda x: -x[0])
    by_id = {r["id"]: r for r in targets}
    taken_p, taken_r, best = set(), set(), {}
    for s, pi, rid in scored:
        if pi in taken_p or rid in taken_r:
            continue
        taken_p.add(pi)
        taken_r.add(rid)
        best[pi] = (s, by_id[rid])
    out = []
    for pi, p in enumerate(pays):
        s, r = best.get(pi, (0.0, None))
        out.append({"payment": p, "row": r, "score": round(s, 2),
                    "amount_ok": abs(p.amount - PRICE) < 0.01})
    return out


def payment_update(via: str, amount: float, paid_at: Optional[datetime] = None,
                   ref: Optional[str] = None, has_cols: bool = True) -> Dict:
    """The column values to write when recording a payment."""
    if not has_cols:
        return {"paid_verified": True}
    upd = {"paid_verified": True, "paid_via": via, "amount_paid": float(amount),
           "paid_at": (paid_at or datetime.now(TZ)).isoformat()}
    if ref:
        upd["payment_ref"] = ref
    return upd


def attendance_update(here: Optional[bool], has_cols: bool = True) -> Dict:
    if not has_cols:
        return {}
    return {"attended": here, "attended_marked_at": datetime.now(TZ).isoformat() if here is not None else None}


def clear_payment_update(has_cols: bool = True) -> Dict:
    if not has_cols:
        return {"paid_verified": False}
    return {"paid_verified": False, "paid_via": None, "amount_paid": None, "paid_at": None, "payment_ref": None}
