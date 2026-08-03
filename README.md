# Petrol Khata

A local web app for petrol pump accounting, built around a single
date-based ledger screen - the way a traditional cash book (*khata*)
works. Log a nozzle meter reading, a receipt, a credit sale, an expense,
a fuel delivery, or a tank dip, and everything else (tank stock, account
balances, cash/bank balances, dashboard totals, reports) updates
automatically.

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

**Change these passwords** before real use, especially on a public
deployment - use the "Change password" link in the sidebar once logged
in, or have the owner reset either one from Settings.

## Business setup wizard

The first time an Owner logs in, Petrol Khata walks through a one-time bootstrap:

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
  nozzle - Petrol Khata calculates liters sold (this reading − the
  previous one) and the sale amount (liters × price) live, and reduces
  the nozzle's tank stock. **Payment / Receipt Received** is one merged
  entry for money coming in from any account - a customer paying down
  what they owe, or an employee repaying a loan; there's no separate
  "customer payment" vs. "employee repayment" form, since the account
  picker already lists every account regardless of type. Bank Sales (the
  portion of today's sales that came in via card/bank, reconciled to a
  specific bank account) is also recorded here.
- **Credit - Customer, Expenses & Purchases**, in order: credit given to
  a customer, cash physically deposited into a bank account, an expense,
  a fuel purchase/delivery into a specific tank (cash or on credit from
  a supplier), a payment made to a supplier against fuel taken on
  credit, and a loan/advance to an employee. A Fuel Purchase's cost is
  required - it can't be saved blank - and shows a live cost-per-liter
  calculation (cost ÷ liters) as you type, so a mistyped cost is easy to
  catch before saving.
- **Dip - Tank Stock Check:** enter each tank's physical dip reading.
  Book stock (starting stock + purchases − sales) is calculated
  automatically; the variance between dip and book stock is shown
  plainly, without being flagged as an error - small differences from
  evaporation, temperature, or rounding are normal.

Right below the date picker, a **sales breakdown** shows Total Sales
(from nozzle readings) minus Credit Sales minus Bank Sales, leaving Cash
Sales as the remainder - the actual physical cash the register should
have taken in that day. Next to it, **liters sold per fuel type** (also
computed from nozzle meter reading differences) shows for the selected
date - the same figures appear on the Daily Report and, plotted over
time, on Trends. Below that (owner only), **Cash in Hand** and every
bank account's balance are shown live - both reflect every transaction
to date (sales, receipts, deposits, expenses, loans, fuel purchases,
supplier payments), not just today's, and update the moment a new entry
is saved.

### Paid via: cash or a specific bank account

**Payment / Receipt Received**, **Loan / Advance to Employee**,
**Expense**, a cash-paid **Fuel Purchase**, and **Payment to Supplier**
each have a "Paid via" field: Cash, or a specific bank account (pick an
existing one or quick-add a new one inline, the same "+ Add new..."
pattern used throughout the Ledger). Choosing a bank account routes that
entry's money through the bank instead of the register:
- A receipt paid via a bank increases that bank's balance instead of
  cash in hand.
- A loan, expense, fuel purchase, or supplier payment paid via a bank
  decreases that bank's balance instead of cash in hand.

Fuel Purchase also has an **"On Credit (from supplier)"** option
alongside "Paid Cash" - selecting it hides "Paid via" and shows a
supplier picker instead, posting the purchase amount to that supplier's
account rather than touching cash or a bank at all (settled later via a
"Payment to Supplier" entry, same as before).

Every account picker lists every account regardless of type (see
"Accounts" below), but sorts its own relevant type first - the Customer
picker shows customers before suppliers/employees, the Supplier picker
shows suppliers first, and so on - so the common case doesn't mean
scrolling past every other type. The one exception is Payment / Receipt
Received's "Account" picker, which is intentionally generic (plain
alphabetical) since it isn't tied to one type.

Each nozzle's reading history forms one continuous chain - a date's
saved current reading automatically becomes the following day's previous
reading. The very first reading ever logged for a nozzle works the same
way as catching up on a missed day: since there's no day before it with
an entry, Petrol Khata asks you to type both the previous and current reading.
The same thing happens for any other date with no entry the day before
it (e.g. backfilling old paper-book records with a gap in them). Once
saved, that typed previous reading is used to fill in the missing day
before it too (as long as *that* day's own previous reading can be
worked out automatically) - so the chain re-links itself as you go, and
you only ever have to type numbers by hand for the specific day where
the trail actually goes cold.

### Fuel prices

A **Fuel Prices** panel (owner only) sits right on the Ledger, showing
each fuel's price as of whichever date is currently selected. Changing
it there takes effect from that date onward - paging back to an earlier
date shows whatever price was actually in effect then, and a nozzle
reading or credit-to-customer entry always prices itself against the
rate for its own `entry_date`, not whatever the price happens to be
today. That means correcting an old reading weeks later re-prices it at
the rate that was actually charged back then, never at today's rate.

Cash in hand can never go negative - any action that would draw it down
past zero on its own date or any later date (an expense, loan, cash-paid
fuel purchase, supplier payment, deposit, or bank-sale reclassification,
whether a new entry or an edit to an existing one) is rejected, showing
how much could still be spent on that date without a later date's
balance going negative. The check runs against the whole timeline rather
than just today's total, which matters when backfilling old paper
records out of order - an entry that's safe for its own day can still
starve a day that comes later in time but was already recorded.

Dashboard, Inventory, and Reports are **read-only** — they reflect
what's been entered on the Ledger; there's no separate place to edit
stock directly. Accounts is the one exception: an account's own detail
page lets the owner edit its details, opening balance, and individual
transactions (see "Accounts" below) - creating a *new* entry, though,
always still happens from the Ledger.

### Shifts

If the pump runs one shift a day, there's nothing to set up - a single
"Full Day" shift is created automatically and stays invisible everywhere
(no selector, no extra clicks). An owner can add more shifts from
**Settings** (e.g. Morning / Evening / Night); once there's more than
one, a shift selector appears on the Ledger, and nozzle readings, credit
sales, and bank sales are all recorded per shift. The meter-reading chain
threads through shifts in order (Morning's closing reading becomes
Evening's opening reading), only rolling to the previous calendar day at
the first shift of the day.

### Cash Handover - shift reconciliation

For each shift, **Cash Handover** on the Ledger shows the cash the
ledger says should have been collected (that shift's sales minus credit
given minus bank sales) next to whatever the owner or attendant actually
counted. This is a check, not a transaction - recording it never changes
any balance, the same way a tank dip doesn't. A real shortfall can be
turned into an actual cash expense with one click ("write off as
expense"), which is what actually reduces cash in hand to match what's
physically there. The Monthly Report totals variance per attendant, so a
one-off shortfall and a repeated pattern read very differently.

### Payroll

**Salary Payment** (owner only, on the Ledger) pays an employee the full
salary they earned, optionally withholding part of it against a loan/
advance they already owe - the deduction can never exceed what they're
actually into the pump for. Only the net amount (salary minus deduction)
leaves cash or a bank account; the deduction settles the account's
balance without any money changing hands twice.

### Tank dip charts

A dip stick measures depth, not volume. A tank with a calibration chart
set up in **Settings** (paste `depth_cm,liters` pairs, one per line, or
straight from a spreadsheet) takes its dip in cm on the Ledger and
converts to liters automatically, interpolating between the points
given. A tank without a chart keeps taking dips in liters directly, same
as before.

## Reports

- **Daily Report** — pick a date to see that day's total sales (by
  nozzle/fuel), cash vs. bank vs. credit split, receipts, expenses,
  salaries, shift cash-handover variances, bank sales, inventory
  received, and stock available per tank.
- **Monthly Report** — a period profit view: revenue against the cost of
  the fuel *actually sold* (COGS, via each fuel's weighted-average
  purchase cost), gross margin, expenses, salaries, net profit, margin by
  fuel type, expenses by category, and cash variance by attendant.
- **Trends** — the same metrics as charts over the past 15 days, month,
  3 months, or year, so patterns are visible over time, including the
  same COGS-based Profit figure as the Monthly Report. Costing the fuel
  actually *sold* rather than subtracting whatever was *bought* that day
  is what stops a large delivery from showing up as a big fake loss on
  the day it arrives - the stock is still sitting in the tank, unsold.

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

Every account's history table shows **who recorded each entry**, and
each account has a **Statement** link - a print/PDF-friendly page for a
chosen date range with the balance carried in, every entry, and the
closing balance, plus an **aging** breakdown (0-30 / 31-60 / 61-90 /
90+ days) for a debitor. Payments are applied against the *oldest*
unpaid entries first, so aging reflects genuinely stale credit rather
than just whatever's most recent. A "How Old Is the Money Owed to You"
panel on the Accounts page totals aging across every debitor at once.

Below the main table, an **Expenses** panel (owner only) lists every
expense ever logged, all-time and in one place - expenses aren't tied to
any account, so this is the only place to see and edit them all
(individual expenses are also editable from whichever bank/cash page
they were paid from, but this is the complete list).

Fuel bought on credit ("On Credit (from supplier)" in the Fuel Purchase
form) is tied to an account. Paying that down is its own Ledger entry
("Payment to Supplier"), which reduces what's owed - mirroring how a
receipt reduces what a customer owes. Employees work the same way in
reverse: a "Loan / Advance to Employee" entry increases what they owe
the pump, and a "Payment / Receipt Received" entry against that same
account reduces it - the same merged receipt entry used for customer
payments.

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

The pump can have multiple named bank accounts (e.g. "Meezan", "HBL").
Create one from **Settings** or from the **Accounts** page (owner
only) - either way it also shows up on Accounts alongside customers,
suppliers, and employees, with its own detail page: transaction
history with a running balance, and (owner only) editable name, opening
balance, and individual entries - the same editing pattern as any other
account, described below. Unlike customers/suppliers/employees, a bank
account's balance isn't a debt relationship, so it never shows up under
the Debitors/Creditors filter - only under "All" or the "Bank Accounts"
type filter.

Ledger entries that affect a bank account's balance:
- **Bank Sales** - the portion of a date's sales that were paid by
  card/bank rather than cash, tagged to the account it landed in.
  Increases that bank's balance and reduces Cash Sales for the day.
- **Cash Deposit** (owner only) - cash physically taken to the bank.
  Increases the chosen bank's balance and decreases cash in hand.
- **Payment / Receipt Received**, when its "Paid via" is set to this
  bank instead of Cash - increases the bank's balance instead of cash
  in hand.
- **Loan / Advance to Employee**, **Expense**, a cash-paid **Fuel
  Purchase**, and **Payment to Supplier**, when "Paid via" is set to
  this bank - each decreases the bank's balance instead of cash in hand.

**Cash in hand** works the same way as a bank account now: it appears on
the Accounts page (as "Cash in Hand", under "All" or its own type
filter - never Debitors/Creditors, since it isn't a debt relationship
either) with its own detail page showing opening balance (settable at
any time, same as any other account) and full transaction history.
Because the biggest contributor - the cash portion of nozzle sales -
isn't a single entry but a derived daily figure (that day's total sales
minus credit given minus bank sales), it shows as one summary row per
date linking to that day's Ledger rather than something directly
editable; fuel purchases and expenses paid in cash are editable right
on the page, and everything else (receipts, loans, supplier payments,
deposits) links back to wherever it's actually edited. The running total
itself is: opening balance, plus every day's cash sales and every
cash-method receipt, minus cash deposited into banks and every
cash-method loan, expense, fuel purchase, and supplier payment. Cash in
hand and every bank account's balance are also shown live on the Ledger
and Daily Report pages (owner only), and on the Dashboard.

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
- For bank accounts: set it when adding the account (Settings or
  Accounts), or any time afterward from the bank account's own page.
- For cash in hand: set it any time from Settings or from its own page
  on Accounts - there's only ever one cash-in-hand account, so it's
  editable rather than something you "add".

## Users &amp; Roles

- **Owner** — full access: everything on the Ledger, Settings, and all
  Reports pages, including adding/deactivating users and resetting
  passwords.
- **Staff** — can log nozzle readings, receipts, customer credit, bank
  sales, employee loans, tank dips, and cash handovers. Cannot log
  expenses, fuel purchases, supplier payments, cash deposits, or
  salaries, cannot edit any account, and cannot see Settings, Reports, or
  live cash/bank balances.

Every user can change their own password from the link in the sidebar.
An owner manages logins from **Settings** - adding a user, resetting
anyone's password, and deactivating (not deleting) an account. Deactivating
rather than deleting is deliberate: every ledger entry records who
recorded it, and that has to keep pointing at a real user for the
"Recorded by" column on every history page to keep making sense. There
always has to be at least one active owner.

## Where your data lives

Everything is stored in `instance/petrolpump.db`. Back this file up
periodically (just copy it) if you want a safety net — it's the only file
that holds your business data. Whether you're running locally or on a
hosted Postgres deployment, **Settings → Backup** gives you the same
safety net from inside the app: a ZIP of every table as CSV, downloadable
with one click.

## Deploying online (Vercel)

Petrol Khata can also run as a hosted web app on [Vercel](https://vercel.com)
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
5. **Deploy.** On first request, Petrol Khata creates its tables in the new
   Postgres database and seeds the default `owner`/`staff` accounts —
   same as the very first local run. Change those passwords immediately
   since the deployment is reachable over the internet.

Schema changes (adding a column, a table, a constraint) ship as Alembic
migrations and apply automatically on startup — there's no need to reset
or re-enter data into an existing database to pick up a new version.
The very first deploy onto a database that already has data (a
production database from before migrations were adopted, or any
existing local `instance/petrolpump.db`) is stamped as already being at
that baseline the first time it boots, since its schema already matches
what the baseline migration creates from scratch.

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
- Cost of Fuel Sold (in the Monthly Report and Trends) uses a
  weighted-average purchase cost per fuel type, not strict FIFO/lot
  tracking - a stable, explainable approximation, not a claim about which
  specific delivery a given liter came from.
- A cash handover is a reconciliation check, not a transaction - counting
  cash never changes cash-in-hand by itself. If a shortfall is real, use
  "write off as expense" on that shift's row to actually reduce the
  balance.
- A nozzle's tank (and therefore its fuel type) can only be reassigned in
  Settings before it has any reading history, since that history doesn't
  store its own fuel type - it's derived from the nozzle's tank live.
- "Net Cash Flow" = nozzle sales − credit given + payments received −
  expenses − cash-paid fuel purchases − supplier payments. Fuel bought on
  supplier credit isn't subtracted at the time of purchase (that cash
  hasn't left yet) - it's counted once the supplier is actually paid.
- A bank account's or cash-in-hand's Receipt, Loan, and Supplier Payment
  entries are only editable from the account they belong to, not from
  the bank/cash page itself (which links to it instead) - Bank Sales and
  Cash Deposits are only editable from the bank's page, and Fuel
  Purchases/Expenses are editable from whichever bank or cash-in-hand
  page they were paid from.
- Cash-in-hand's "Cash Sales" rows (the cash portion of a date's nozzle
  sales) are a derived daily figure, not a single entry, so they're
  read-only on cash-in-hand's page - open that date's Ledger to correct
  the underlying reading, credit, or bank sale instead.
- Editing an account's transaction entry can change its date, amount,
  paid-via, and other details, but not which account it's attached to
  (or, for a fuel purchase, whether it was paid cash/bank vs. on
  credit). There's also no delete - to correct an entry logged against
  the wrong account, log an offsetting entry instead.
