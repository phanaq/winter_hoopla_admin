# Hoopla Admin (payments, attendance, refunds)

`admin.py` is a password-protected Streamlit app for the organizer. It's built for a phone at the
field. The pure logic (roster, statuses, refund suggestions, CSV parsing and matching) is in
`admin_logic.py`.

## 1. Apply the migration (once)

Open the Supabase dashboard, go to **SQL Editor**, paste in
`supabase/migrations/20261111020000_payment_tracking.sql` from the **winter_hoopla** project
(the signup app's repo keeps all database migrations) and run it. You can run it more than
once safely. It adds these columns to `signups`: `amount_paid`, `paid_via`, `paid_at`,
`payment_ref`, `refund_status`, `refund_amount`, `refunded_at`, `admin_notes`, `attended` and
`attended_marked_at`.

The app still works before the migration, but it shows a yellow **"Run the migration"** banner and
can only mark people paid (`paid_verified`). After you run it, tap **"I ran it — recheck"**.

Waivers need a second migration, `supabase/migrations/20261113000000_waivers.sql`. It creates a
`waivers` table with one row per player (keyed by their lowercased signup email), because a signed
waiver covers them for every week and season.

## 2. Deploy on Streamlit Community Cloud (second app)

1. Go to share.streamlit.io, choose **Create app**, pick this repo (`winter_hoopla_admin`) and branch `main`, and set
   **Main file path** to `admin.py`. Pick a different URL from the signup app.
2. Under **Advanced settings → Secrets**, paste:

   ```toml
   [supabase]
   url = "https://YOUR-PROJECT.supabase.co"
   key = "YOUR-SECRET-KEY"        # same secret key the signup app uses

   [admin]
   password = "pick-a-long-password"
   ```

3. Set the Python version to 3.9 or later (the same as the signup app). No extra packages are needed.

If the `[admin]` password is missing, the app shows an error and stops. It never runs without a
password. You have to log in again after closing or reloading the tab.

## 3. Run locally

Copy `.streamlit/secrets.toml.template` to `.streamlit/secrets.toml` and fill it in, then:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/streamlit run admin.py --server.port 8502
```

## Using it

- **At the field** (the default screen): opens on today's or the next session (Chicago time).
  The top line shows how many people are here, $ collected and $ owed. People who still owe are
  listed first, and the **Owes me** filter shows only them. Each player has two buttons:
  - **📝 Got waiver** shows instead of **Here** (with "⚠️ no waiver" on the card) until the
    player has signed the waiver. Players only sign it once, ever: after you tap it they never
    show as missing a waiver again, in any week. Undo it from the player's **Mark paid** panel.
  - **✋ Here** and **🚫 No-show** mark attendance. Tap **↩ Undo** to clear either one.
  - **💰 Mark paid** opens a panel with venmo/zelle/cash (it starts on whatever they declared),
    the amount (default $20) and **Record payment for NAME**. The same panel has a note field
    and **No-show**.

  Every tap saves that one player right away, so two phones stay in sync. Tap **↻** to pick up
  changes made on the other phone. Waitlisted players are in the collapsed **Waitlist** section.
  You can mark them here or paid too, in case they got pulled in at the field.
- **Status badges:** ✅ Paid · ❓ Says Venmo/Zelle — verify · 💵 Owes $20 (in person) ·
  ⚠️ Nothing declared (moved up from the waitlist, so they were only emailed payment options).
- **Refunds:** built automatically from cancellations. Tap **Update refund** to mark someone
  refunded (amount and date), owed, or not needed.
- **Season:** one row per player with rostered weeks, weeks they showed up, no-shows, waitlisted
  weeks, cancellations, paid $, owed $ and refunds.
- **Export:** CSVs of the selected week, every row, or the season overview.

## Venmo import

1. In Venmo (the website works best), open **Statements**, pick the month and download the CSV.
2. In the app, choose the week at the top, then **Import**, then **Venmo statement**, and upload
   the file.
3. The app reads only completed payments coming in to you. It skips the title rows, summary rows,
   transfers and your own outgoing payments. For each payment it suggests an unpaid rostered
   player from that week, using:
   - **Name:** fuzzy match on the Venmo name (for example "Jane Doe" matches "Jane D." or "jane").
   - **Date:** a play date in the note ("11/23", "Nov 23", "week 2") wins. Payments with no date
     only count from the Monday before to the Thursday after that session.
4. Check or change each match in the dropdowns, then tap **Record selected payments**. Amounts
   other than $20 are flagged. A $40 "2 weeks" payment is recorded for this week only, so add a
   note for the other week. Each Venmo transaction ID is saved in `payment_ref`, so importing the
   same file again won't record anything twice.

**Zelle / bank CSV:** any CSV with a date column and an amount column, plus a name column or a
description like "Zelle payment from JANE DOE Conf# …". Only positive amounts are used.

## How refunds are suggested

For each cancelled signup, the app rebuilds that session's roster as it stood just before the
cancellation. That means the rows created before `cancelled_at` that weren't already cancelled,
with each side taking its first 10 in id order.

- A payment was recorded → **owed**
- Was on the roster and declared Venmo/Zelle → **owed** (check you actually got it)
- Was on the waitlist, chose pay in person, or never declared → **not needed**

Anything you set by hand overrides the suggestion.
