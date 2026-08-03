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

**Change these passwords** before real use — there's no self-service
"change password" screen yet.

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
past zero (an expense, loan, cash-paid fuel purchase, supplier payment,
deposit, or bank-sale reclassification, whether a new entry or an edit
to an existing one) is rejected with the current available balance
shown, rather than letting the register go negative.

Dashboard, Inventory, and Reports are **read-only** — they reflect
what's been entered on the Ledger; there's no separate place to edit
stock directly. Accounts is the one exception: an account's own detail
page lets the owner edit its details, opening balance, and individual
transactions (see "Accounts" below) - creating a *new* entry, though,
always still happens from the Ledger.

## Reports

- **Daily Report** — pick a date to see that day's total sales (by
  nozzle/fuel), cash vs. bank vs. credit split, receipts, expenses, bank
  sales, inventory received, and stock available per tank.
- **Trends** — the same metrics as charts over the past 15 days, month,
  3 months, or year, so patterns are visible over time, plus a
  **Profit (Est.)** figure and chart: sales revenue minus fuel purchase
  cost minus expenses, cash-basis. It's an estimate, not a strict
  accounting profit/margin figure - it isn't adjusted for fuel bought but
  not yet sold.

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

## Roles

- **Owner** — full access: everything on the Ledger, Settings, and both
  Reports pages.
- **Staff** — can log nozzle readings, receipts, customer credit, bank
  sales, and employee loans, and tank dips. Cannot log expenses, fuel
  purchases, supplier payments, or cash deposits, cannot edit any
  account, and cannot see Settings, Reports, or live cash/bank balances.

## Where your data lives

Everything is stored in `instance/petrolpump.db`. Back this file up
periodically (just copy it) if you want a safety net — it's the only file
that holds your business data.

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
