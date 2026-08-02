# Munchi

A local web app for petrol pump accounting, built around a single
date-based ledger screen - the way a traditional cash book works. Log a
nozzle meter reading, a customer payment, a credit sale, an expense, a
fuel delivery, or a tank dip, and everything else (tank stock, customer
balances, dashboard totals, reports) updates automatically.

## What this is built with

- **Python + Flask** — runs a small web server on your own computer.
- **SQLite** — a real database stored in a single file
  (`instance/petrolpump.db`). Your data is saved permanently and survives
  restarts; it is not stored "in the browser".
- No internet connection is required to run it — everything is local.
  Charts on the Trends page are plain server-drawn SVG, not a JS library.

## First-time setup (do this once)

1. Open a terminal in this folder.
2. Install the required packages:

   ```bash
   pip install -r requirements.txt
   ```

## Running the app

```bash
python app.py
```

Then open **http://localhost:5000** in your browser. Leave the terminal
window open while you use the app; closing it stops the server.

## Default accounts

On first run, two accounts are created automatically (shown once in the
terminal as well):

| Role  | Username | Password  |
|-------|----------|-----------|
| Owner | owner    | owner123  |
| Staff | staff    | staff123  |

**Change these passwords** before real use — there's no self-service
"change password" screen yet.

## Business setup wizard

The first time an Owner logs in, Munchi walks through a one-time bootstrap:

1. **Tanks** — add each physical storage tank: its fuel type, capacity,
   and current stock. You can add more than one tank of the same fuel
   (e.g. two separate Diesel tanks).
2. **Fuel prices** — one price per fuel type.
3. **Dispensers & nozzles** — how many dispensers, how many nozzles each,
   and which specific tank each nozzle draws from.

That's all setup covers - it never asks for or stores a meter reading.
The first reading for every nozzle is entered later, from the Ledger,
the same way any other day's reading is (see below).

This bootstrap only runs once. After that, an Owner can add more tanks,
dispensers, and nozzles, or edit fuel prices and tank capacity/alert
levels, any time from **Settings** - which likewise never asks for a
meter reading. Staff accounts can't use the app until an Owner finishes
the initial bootstrap.

## The Ledger - the main screen

Everyone lands on the **Ledger** after logging in. A date picker at the
top loads that date's entries - work on today, or go back and fill in
(or correct) a past date, e.g. when backfilling old paper-book records.
Re-saving a nozzle reading or tank dip for a date that already has one
updates it in place instead of duplicating it.

- **Debit - Sales & Receipts:** enter the date's meter reading for each
  nozzle - Munchi calculates liters sold (this reading − the previous
  one) and the sale amount (liters × price) live, and reduces the
  nozzle's tank stock. Also where a customer's payment, an employee's
  loan repayment, and bank sales (the portion of today's sales that
  came in via card/bank, reconciled to a specific bank account) are
  recorded.
- **Credit - Customer, Expenses & Purchases:** credit given to a
  customer, a loan/advance to an employee, an expense, a fuel
  purchase/delivery into a specific tank (cash or on credit from a
  supplier), a payment made to a supplier against fuel taken on credit,
  or cash physically deposited into a bank account.
- **Dip - Tank Stock Check:** enter each tank's physical dip reading.
  Book stock (starting stock + purchases − sales) is calculated
  automatically; the variance between dip and book stock is shown
  plainly, without being flagged as an error - small differences from
  evaporation, temperature, or rounding are normal.

Right below the date picker, a **sales breakdown** shows Total Sales
(from nozzle readings) minus Credit Sales minus Bank Sales, leaving Cash
Sales as the remainder - the actual physical cash the register should
have taken in that day.

Each nozzle's reading history forms one continuous chain - a date's
saved current reading automatically becomes the following day's previous
reading. The very first reading ever logged for a nozzle works the same
way as catching up on a missed day: since there's no day before it with
an entry, Munchi asks you to type both the previous and current reading.
The same thing happens for any other date with no entry the day before
it (e.g. backfilling old paper-book records with a gap in them). Once
saved, that typed previous reading is used to fill in the missing day
before it too (as long as *that* day's own previous reading can be
worked out automatically) - so the chain re-links itself as you go, and
you only ever have to type numbers by hand for the specific day where
the trail actually goes cold.

Dashboard, Inventory, Accounts, and Reports are **read-only** — they
reflect what's been entered on the Ledger; there's no separate place to
edit stock or any account's balance directly.

## Reports

- **Daily Report** — pick a date to see that day's total sales (by
  nozzle/fuel), cash vs. bank vs. credit split, receipts, expenses, bank
  sales, inventory received, and stock available per tank.
- **Trends** — the same metrics as charts over the past 15 days, month,
  3 months, or year, so patterns are visible over time.

## Accounts (customers, suppliers, employees)

The **Accounts** page is a single ledger for every relationship that
carries a balance. Under the hood there's just one account pool -
"customer", "supplier", and "employee" are simple labels for organizing
and searching, not separate silos. Any account can receive any kind of
entry: the same account picker on the Ledger (for customer credit,
supplier purchases, employee loans, everything) lists every account, so
an account labelled "supplier" can still be given customer credit, and
vice versa. This is for cases like two petrol pumps that occasionally
sell fuel to each other - one account, one running balance, instead of
needing a separate customer record and a separate supplier record for
the same business.

A balance is positive when the account owes the pump money (a debitor)
and negative when the pump owes the account money (a creditor) -
regardless of its type label. The **Creditors / Debitors** filter at the
top of the Accounts page is based purely on this current balance sign,
so an account's classification there can shift over time as its balance
shifts. The **Account type** filter narrows by label only.

Fuel bought on credit ("On Credit (from supplier)" in the Fuel Purchase
form) is tied to an account. Paying that down is its own Ledger entry
("Payment to Supplier"), which reduces what's owed - mirroring how a
customer receipt reduces what a customer owes. Employees work the same
way in reverse: a "Loan / Advance to Employee" entry increases what they
owe the pump, and "Repayment from Employee" reduces it.

### Editing an account

Click into any account from the Accounts page (owner only) to:
- Edit its name, phone, or type label.
- Add or update its opening balance and as-of date at any time - not
  just when the account is first created.
- Edit any individual entry in its transaction history (amount, date,
  fuel/tank/liters where relevant, note) via the "Edit" link on that row.

Every balance shown - the account's current balance, and the running
balance next to each row in its history - is recalculated fresh from the
full chronological list of entries every time the page loads, the same
way tank stock is never stored as a mutable counter (see "Where your
data lives" below). That means editing an entry from any point in the
account's history, including the opening balance itself, always
correctly ripples forward through every later entry's running balance
and the account's current total - there's nothing cached to go stale.

## Bank accounts &amp; cash in hand

The pump can have multiple named bank accounts (e.g. "Meezan", "HBL"),
each with its own balance, managed from **Settings**. Two Ledger entries
affect them:
- **Bank Sales** - the portion of a date's sales that were paid by
  card/bank rather than cash, tagged to the account it landed in.
  Increases that bank's balance and reduces Cash Sales for the day.
- **Cash Deposit** (owner only) - cash physically taken to the bank.
  Increases the chosen bank's balance and decreases cash in hand.

**Cash in hand** is a single running total (Settings &gt; "Cash in
Hand"): opening balance, plus every day's cash sales, minus cash
deposited into banks. Both bank accounts and cash in hand are visible
on the Dashboard.

## Opening balances

Every account type - customers, suppliers, employees, bank accounts,
and cash in hand - can be given an opening balance dated as of a chosen
day. This is for pumps migrating from paper records that already have
real balances: outstanding customer/supplier/employee dues, money
already in the bank, cash already in the register. It shows up as the
first entry in that account's history, dated on the day you specify, so
everything after it reads as a correct running balance.

- For customers, suppliers, and employees: set it on the account's own
  detail page (owner only) - either when first creating it via "Add an
  Account" on the Accounts page, or any time afterward from the account's
  page. Quick-adding a new account inline from the Ledger (e.g. "+ Add
  new customer") always starts at zero - use the Accounts page or the
  account's own page instead when it already has a real balance.
- For bank accounts: set it when adding the account in Settings.
- For cash in hand: set it any time from Settings - there's only ever
  one cash-in-hand account, so it's editable rather than something you
  "add".

## Roles

- **Owner** — full access: everything on the Ledger, Settings, and both
  Reports pages.
- **Staff** — can log nozzle readings, customer payments/credit, bank
  sales, and employee loans/repayments, and tank dips. Cannot log
  expenses, fuel purchases, supplier payments, or cash deposits, and
  cannot see Settings or Reports.

## Where your data lives

Everything is stored in `instance/petrolpump.db`. Back this file up
periodically (just copy it) if you want a safety net — it's the only file
that holds your business data.

## Deploying online (Vercel)

Munchi can also run as a hosted web app on [Vercel](https://vercel.com)
instead of (or alongside) running locally. Vercel's serverless functions
have no persistent disk, so this mode swaps the local SQLite file for a
real hosted Postgres database — everything else (routes, templates,
ledger logic) is unchanged and controlled purely by environment
variables, so local `python app.py` still works exactly as before.

1. **Push this folder to a GitHub repo** (Vercel deploys from Git).
2. **Sign up / log in at [vercel.com](https://vercel.com)** and import
   that GitHub repo as a new Project. Vercel auto-detects `vercel.json`
   and the `api/index.py` entry point — no build configuration needed.
3. **Add a Postgres database**: in the Vercel project, go to the
   **Storage** tab → **Create Database** → Postgres (or connect a Neon/
   Supabase database you already have). Vercel adds a `DATABASE_URL`
   environment variable to the project automatically.
4. **Set a `SECRET_KEY` environment variable** (Project → Settings →
   Environment Variables) to a long random string — this keeps login
   sessions valid across deployments. Without it the app still runs, but
   a fresh key is generated on every cold start and everyone gets logged
   out constantly.
5. **Deploy.** On first request, Munchi creates its tables in the new
   Postgres database and seeds the default `owner`/`staff` accounts —
   same as the very first local run. Change those passwords immediately
   since the deployment is reachable over the internet.

Note the multi-user implication of a real deployment: everyone hitting
the same URL shares one Postgres database (this was already true of the
single shared SQLite file for anyone using the same computer — just now
reachable remotely instead of only on one PC). Back up the Postgres
database periodically the same way you'd back up `instance/petrolpump.db`.

## Known limitations

- The automatic chain-backfill only reaches back one day at a time. If
  more than one consecutive day is missing before the date you're
  entering, only the single day immediately before it gets filled in
  automatically - the rest stay empty until you visit those dates
  directly and fill them in yourself (each one just needs the same
  "enter both readings" step once).
- Editing an already-saved date doesn't automatically recalculate a
  *later* date's stored totals if that later date depended on it - open
  that later date and re-save its readings to reconcile them.
- Fuel prices apply from whenever they're changed in Settings; the app
  doesn't keep a history of past prices, so backfilled sales use
  whatever the price is *now*, not the historical price on that date.
- "Net Cash Flow" = nozzle sales − credit given + payments received −
  expenses − cash-paid fuel purchases − supplier payments. Fuel bought on
  supplier credit isn't subtracted at the time of purchase (that cash
  hasn't left yet) - it's counted once the supplier is actually paid.
- Editing an account's transaction entry can change its date, amount,
  and other details, but not which account it's attached to or (for a
  fuel purchase) whether it was paid cash vs. on credit. There's also no
  delete - to correct an entry logged against the wrong account or with
  the wrong payment method, log an offsetting entry instead.
