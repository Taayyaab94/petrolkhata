"""Accounts routes: the accounts list/detail pages, adding/editing/
deleting accounts (customers, suppliers, employees, owner draws) and bank
accounts, cash-in-hand, per-account statements and CSV/PDF export, and the
edit handlers for ledger entries reached from an account's own page
(credit, receipt, purchase, supplier payment, employee loan, salary,
bank sale, cash deposit, expense, fuel purchase).

Moved verbatim out of app.py - this is the accounts/... route group plus
the two edit-validation helpers used only by it, unchanged except for
their location.
"""

from datetime import date

from flask import (
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import login_required

from formatting import format_number
from extensions import db
from ledger_logic import (
    account_ledger_events,
    bank_account_ledger_events,
    cash_account_balance,
    cash_account_ledger_events,
    credit_aging,
    price_on_date,
    price_resolver,
)
from models import (
    Account,
    BankAccount,
    BankSale,
    CashDeposit,
    CreditGiven,
    EmployeeLoan,
    Expense,
    FuelType,
    Receipt,
    SalaryPayment,
    StockPurchase,
    SupplierPayment,
    Tank,
)
from app import (
    app,
    _credit_amount_error,
    _derive_credit_liters_amount,
    _resolve_entry_mode,
    _resolve_export_format,
    _send_export,
    cash_shortfall_message,
    get_cash_account,
    owner_required,
    parse_date_param,
    resolve_payment_method,
    slugify,
    would_overdraw_cash,
)


def _validate_amount_cash_edit(entry, entry_date, amount):
    """Shared amount/cash-overdraw validation for the plain "amount only"
    edit handlers (bank sale, cash deposit) whose bodies were otherwise
    identical. Returns the flash error text, or None if the edit is
    valid."""
    if not amount or amount <= 0:
        return "Amount must be a positive number."
    if would_overdraw_cash(amount, entry_date, entry.amount, entry.entry_date):
        return cash_shortfall_message(entry_date)
    return None


def _validate_cash_payment_edit(entry, entry_date, amount, form):
    """Shared amount/payment-method/cash-overdraw validation for the
    simple "amount + paid_via" edit handlers (supplier payment, employee
    loan) whose bodies were otherwise identical. Returns
    (method, bank_account, error) where error is the flash text to show
    on failure, or None if the edit is valid."""
    method, bank_account, method_error = resolve_payment_method(form)
    old_cash_amount = entry.amount if entry.method == "cash" else 0
    new_cash_amount = amount if (amount and method == "cash") else 0

    if not amount or amount <= 0:
        error = "Amount must be a positive number."
    elif method_error:
        error = method_error
    elif would_overdraw_cash(new_cash_amount, entry_date, old_cash_amount, entry.entry_date):
        error = cash_shortfall_message(entry_date)
    else:
        error = None
    return method, bank_account, error



# ------------------------------------------------------------ accounts ---

ACCOUNT_TYPES = ("customer", "supplier", "employee", "owner")


def _validate_parent(account, parent_id):
    """Guard the parent/sub-account link. Returns an error string to flash,
    or None when the link is allowed.

    Sub-accounts are ONE LEVEL DEEP and nothing in the database enforces
    that (the FK is self-referential and would happily allow a chain, or
    even a cycle) - these three checks are the only thing keeping the tree
    flat, which is what lets group_balance stay a single non-recursive sum
    and lets every balance/aging consumer keep treating a sub-account as
    an ordinary account.

    account may be None (the add-account path, where the row does not
    exist yet) - the self-parent and has-own-children checks simply do not
    apply to a brand new account.

    A parent id that matches no account is treated as "no parent" rather
    than an error: Account.query is already tenant-scoped (see tenancy.py),
    so an id belonging to another pump resolves to None here and can never
    become a cross-pump parent - there is nothing extra to re-check.
    """
    if parent_id is None:
        return None

    if account is not None and parent_id == account.id:
        return "An account can't be its own parent."

    parent = db.session.get(Account, parent_id)
    if parent is None:
        return None

    if parent.parent_account_id is not None:
        return f"{parent.name} is already a sub-account - sub-accounts are only one level deep."

    if account is not None and account.children:
        return f"{account.name} has its own sub-accounts, so it can't become one."

    return None


def _parent_id_from_form(form, field="parent_account_id"):
    """Read the optional parent picker off a form. An empty string (the
    blank first option) means "top-level", i.e. None - which is also what
    clearing an existing link submits, so editing an account back to
    top-level works through the same path."""
    raw = (form.get(field) or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _group_aging(account, children, as_of_date):
    """The aging buckets shown on ONE Accounts-list row.

    For a childless account this is plain credit_aging() and nothing has
    changed. For a parent - whose children are not listed separately on
    that page - the row's buckets are its own plus each child's, summed
    bucket by bucket, with the worst (largest) oldest_days of the group,
    so the "Oldest Unpaid" badge reflects the oldest money anywhere in the
    group rather than only the parent's own.

    This is a per-row DISPLAY aggregate only. The page's aging_totals are
    built separately from every account individually (see the invariant
    comment in _accounts_context) - never from these row figures."""
    own = credit_aging(account, as_of_date) if account.balance > 0 else None
    if not children:
        return own

    buckets = {"0-30": 0.0, "31-60": 0.0, "61-90": 0.0, "90+": 0.0}
    oldest_days = None
    oldest_date = None
    parts = [own] + [credit_aging(c, as_of_date) if c.balance > 0 else None for c in children]
    found = False
    for part in parts:
        if not part:
            continue
        found = True
        for bucket, value in part["buckets"].items():
            buckets[bucket] = round(buckets[bucket] + value, 2)
        if part["oldest_days"] is not None and (oldest_days is None or part["oldest_days"] > oldest_days):
            oldest_days = part["oldest_days"]
            oldest_date = part["oldest_date"]
    if not found:
        return None
    return {
        "buckets": buckets,
        "outstanding": round(sum(buckets.values()), 2),
        "oldest_date": oldest_date,
        "oldest_days": oldest_days,
    }


def _accounts_context(kind, type_filter, q=""):
    """Shared by the Accounts page and its PDF/Excel export, so the two
    can never quietly drift apart - same filters, same rows, same aging
    totals, just rendered differently.

    q is the name search box. It is applied HERE, not in the view, for
    that same reason: an export that silently ignored the search would
    hand the owner a document that does not match the screen it was
    exported from. It filters by name only, case-insensitively, on a
    plain "contains" match - a khata search is someone half-remembering a
    name, not a query language.

    WHAT q DELIBERATELY DOES NOT TOUCH: aging_totals. Those are the
    headline "how old is the money owed to you" figures and are computed
    from every account in the type filter (see the invariant note further
    down). Narrowing them to whatever the owner happened to type into a
    search box would make a headline total mean something different from
    one keystroke to the next.
    """
    q = (q or "").strip()

    def matches_search(name):
        """Case-insensitive substring match, done in Python rather than as
        a SQL ilike so bank accounts and the synthetic Cash-in-Hand row -
        which are not Account rows and never go through that query - are
        filtered by exactly the same rule as accounts are."""
        return not q or q.casefold() in (name or "").casefold()
    if type_filter in ("bank", "cash"):
        # Debtor/creditor is a concept that only applies to customer/
        # supplier/employee accounts - bank accounts and cash-in-hand are
        # the pump's own money, not a relationship with someone else, so
        # they always show under "All" regardless of a stale kind= param.
        kind = "all"

    rows = []
    if type_filter not in ("bank", "cash"):
        # TOP-LEVEL ONLY: a sub-account is reached through its parent's
        # detail page, never listed here in its own right. That is also
        # what stops this list double-counting - a parent row shows the
        # rolled-up group figure, so listing its children alongside it
        # would show the same money twice.
        query = Account.query.filter(Account.parent_account_id.is_(None))
        if type_filter in ACCOUNT_TYPES:
            query = query.filter_by(account_type=type_filter)
        for a in query.all():
            # Debitor/creditor is purely a function of the account's current
            # balance sign - not its type label - so an account's
            # classification here can shift over time as its balance shifts.
            children = a.children
            # A childless account shows its OWN balance exactly as before.
            # A parent shows the group total, because its children are not
            # on this list to be seen separately - and every filter and
            # sort below then works off whatever is actually displayed.
            # Searched on the PARENT's name only, matching what the page
            # lists: a sub-account is not a row here, so matching one
            # would surface a row whose name does not contain the search
            # text and give no clue why.
            if not matches_search(a.name):
                continue
            balance = a.group_balance if children else a.balance
            aging = _group_aging(a, children, date.today())
            rows.append(
                {
                    "kind": "account",
                    "obj": a,
                    "name": a.name,
                    "balance": balance,
                    "aging": aging,
                    "child_count": len(children),
                    "is_group": bool(children),
                    "notes": a.notes,
                }
            )

    if kind == "all" and type_filter in ("all", "bank"):
        for b in BankAccount.query.all():
            # Bank accounts (and cash-in-hand) are the pump's own money,
            # not a debitor/creditor relationship, so they only show up
            # under "All" - not under the Debitors/Creditors filter.
            if not matches_search(b.name):
                continue
            rows.append({"kind": "bank", "obj": b, "name": b.name, "balance": b.balance})

    if kind == "all" and type_filter in ("all", "cash") and matches_search("Cash in Hand"):
        cash_account = get_cash_account()
        rows.append(
            {
                "kind": "cash",
                "obj": cash_account,
                "name": "Cash in Hand",
                "balance": cash_account_balance(cash_account),
            }
        )

    if kind == "debitors":
        rows = [r for r in rows if r["balance"] > 0]
    elif kind == "creditors":
        rows = [r for r in rows if r["balance"] < 0]

    rows.sort(key=lambda r: r["name"].lower())

    expenses = Expense.query.order_by(Expense.entry_date.desc(), Expense.recorded_at.desc()).all()
    bank_accounts = BankAccount.query.order_by(BankAccount.name).all()
    # Every top-level account, for the Add-an-Account form's optional
    # parent picker - deliberately NOT `rows` above, which is narrowed by
    # the page's type/kind filters and would otherwise hide perfectly
    # valid parents just because the owner is looking at "Suppliers".
    parent_candidates = (
        Account.query.filter(Account.parent_account_id.is_(None)).order_by(Account.name).all()
    )

    # Aging totals across every debitor, so overdue money is visible at a
    # glance instead of one account at a time.
    #
    # INVARIANT - these totals are computed from EVERY account, including
    # sub-accounts, deliberately NOT from `rows` above (which is filtered
    # to top-level accounts and carries rolled-up group figures). "Total
    # owed to you" has to keep meaning exactly what it means today: every
    # account counted once. Summing the rows instead would either drop
    # sub-accounts from a type/kind-filtered list or double-count them
    # against their parent's group figure - both of which would silently
    # change a headline number that nothing about grouping should touch.
    aging_query = Account.query
    if type_filter in ACCOUNT_TYPES:
        aging_query = aging_query.filter_by(account_type=type_filter)
    aging_totals = {"0-30": 0.0, "31-60": 0.0, "61-90": 0.0, "90+": 0.0}
    # Only a debit balance ages, so the Creditors view has nothing to
    # total here - exactly as before, when every creditor row carried
    # aging=None.
    if type_filter not in ("bank", "cash") and kind != "creditors":
        today_ = date.today()
        for a in aging_query.all():
            if a.balance <= 0:
                continue
            aging = credit_aging(a, today_)
            for bucket, value in aging["buckets"].items():
                aging_totals[bucket] = round(aging_totals[bucket] + value, 2)

    # The summary band at the top of the page. Computed from the rows
    # actually on screen - unlike aging_totals above, which is a headline
    # figure with a fixed meaning - because this band is a description OF
    # THIS LIST, so it has to move with the filters and the search or it
    # would be describing a list nobody is looking at.
    #
    # BANK AND CASH ROWS ARE EXCLUDED FROM THE MONEY FIGURES, deliberately.
    # They are the pump's OWN money, not a debitor/creditor relationship
    # with anyone (the same reason they only ever appear under "All", and
    # the same reason the page says so in prose). Folding a negative cash
    # balance into "You Owe" would claim the pump owes its own till money.
    # They still count as rows, though - the count describes the list.
    party_rows = [r for r in rows if r["kind"] == "account"]
    total_owed_to_you = round(sum(r["balance"] for r in party_rows if r["balance"] > 0), 2)
    total_you_owe = round(-sum(r["balance"] for r in party_rows if r["balance"] < 0), 2)

    return {
        "rows": rows,
        "kind": kind,
        "type_filter": type_filter,
        "q": q,
        "summary": {
            "owed_to_you": total_owed_to_you,
            "you_owe": total_you_owe,
            "net": round(total_owed_to_you - total_you_owe, 2),
            "count": len(rows),
        },
        "expenses": expenses,
        "bank_accounts": bank_accounts,
        "parent_candidates": parent_candidates,
        "aging_totals": aging_totals,
        "aging_total": round(sum(aging_totals.values()), 2),
    }


@app.route("/accounts")
@login_required
def accounts():
    kind = request.args.get("kind", "all")
    type_filter = request.args.get("type", "all")
    q = request.args.get("q", "")
    ctx = _accounts_context(kind, type_filter, q)
    return render_template("accounts.html", today=date.today(), **ctx)


@app.route("/accounts/export")
@login_required
def accounts_export():
    """Mirrors accounts() - same kind/type filters, same name search, same
    rows and aging totals - as either a PDF or an Excel workbook. Available to staff too,
    matching the page itself (owner-only content there is limited to the
    Expenses/Add-account panels, which this export doesn't include)."""
    fmt = _resolve_export_format()
    kind = request.args.get("kind", "all")
    type_filter = request.args.get("type", "all")
    q = request.args.get("q", "")
    ctx = _accounts_context(kind, type_filter, q)

    def oldest_label(row):
        aging = row.get("aging")
        if aging and aging.get("oldest_days") is not None:
            return f"{aging['oldest_days']} days"
        return "-"

    table_rows = []
    for r in ctx["rows"]:
        type_label = {"bank": "Bank Account", "cash": "Cash"}.get(r["kind"]) or r["obj"].account_type.capitalize()
        phone = (r["obj"].phone or "-") if r["kind"] == "account" else "-"
        table_rows.append([r["name"], type_label, phone, r["balance"], oldest_label(r)])

    blocks = [
        {
            "type": "table",
            "heading": "Accounts",
            "columns": ["Name", "Type", "Phone", "Balance (Rs)", "Oldest Unpaid"],
            "rows": table_rows,
            "align": ["left", "left", "left", "right", "left"],
        },
        {"type": "note", "text": "How old is the money owed to you - across every account with a positive balance."},
        {
            "type": "summary",
            "rows": [
                ("0-30 days (Rs)", ctx["aging_totals"]["0-30"]),
                ("31-60 days (Rs)", ctx["aging_totals"]["31-60"]),
                ("61-90 days (Rs)", ctx["aging_totals"]["61-90"]),
                ("Over 90 days (Rs)", ctx["aging_totals"]["90+"]),
                ("Total Owed To You (Rs)", ctx["aging_total"]),
            ],
        },
    ]

    filter_bits = []
    if kind != "all":
        filter_bits.append(kind)
    if type_filter != "all":
        filter_bits.append(type_filter)
    if ctx["q"]:
        filter_bits.append('search "' + ctx["q"] + '"')
    subtitle = f"As of {date.today().isoformat()}" + (f" - {' / '.join(filter_bits)}" if filter_bits else "")

    return _send_export(
        fmt,
        pdf_title="Accounts",
        pdf_subtitle=subtitle,
        xlsx_sheet_name="Accounts",
        blocks=blocks,
        filename_base=f"petrol-khata-accounts-{date.today().isoformat()}",
    )


@app.route("/accounts/add", methods=["POST"])
@login_required
@owner_required
def accounts_add():
    account_type = request.form.get("account_type", "")
    name = request.form.get("name", "").strip()
    phone = request.form.get("phone", "").strip()
    opening_balance = request.form.get("opening_balance", type=float) or 0
    raw_date = request.form.get("opening_balance_date", "").strip()
    opening_balance_date = parse_date_param(raw_date) if raw_date else None
    # Optional grouping - blank means an ordinary top-level account, which
    # is what every account was before this field existed.
    parent_account_id = _parent_id_from_form(request.form)
    parent_error = _validate_parent(None, parent_account_id)

    if not name:
        flash("Please enter a name.", "error")
    elif account_type == "bank":
        db.session.add(
            BankAccount(
                name=name,
                opening_balance=opening_balance,
                opening_balance_date=opening_balance_date,
            )
        )
        db.session.commit()
        flash(f"Added bank account \"{name}\".", "success")
    elif account_type not in ACCOUNT_TYPES:
        flash("Please choose an account type.", "error")
    elif parent_error:
        flash(parent_error, "error")
    else:
        db.session.add(
            Account(
                name=name,
                phone=phone or None,
                account_type=account_type,
                opening_balance=opening_balance,
                opening_balance_date=opening_balance_date,
                parent_account_id=parent_account_id,
            )
        )
        db.session.commit()
        flash(f"Added {account_type} \"{name}\".", "success")

    return redirect(url_for("accounts"))


@app.route("/accounts/<int:account_id>")
@login_required
def account_detail(account_id):
    account = db.session.get(Account, account_id) or abort(404)
    events = account_ledger_events(account)
    fuel_types = FuelType.query.order_by(FuelType.name).all()
    # A resolver bulk-loads FuelPriceHistory once instead of a query per
    # (entry, fuel type) pair - price_on_date() would be entries x fuel
    # types SELECTs here, which is fine for a handful of rows but an N+1
    # storm on an account with a long credit history (see price_resolver()).
    resolve_price = price_resolver(fuel_types)
    for e in events:
        if e["kind"] == "credit":
            # The edit form's fuel dropdown must label each option with the
            # rate in force on THIS entry's own date, not today's cached
            # price - otherwise editing an old entry shows a price that was
            # never actually charged (see price_on_date()). Only credit
            # entries have an edit form with a fuel picker, so no other kind
            # needs this.
            e["fuel_prices"] = {f.id: resolve_price(f, e["entry_date"]) for f in fuel_types}
    tanks = Tank.query.order_by(Tank.number).all()
    bank_accounts = BankAccount.query.order_by(BankAccount.name).all()
    today = date.today()
    children = sorted(account.children, key=lambda c: c.name.lower())
    # The edit form's parent picker only ever offers TOP-LEVEL accounts
    # other than this one - offering a sub-account would create a second
    # level, and offering itself would create a self-parent. Both are
    # rejected by _validate_parent() anyway; this just keeps the dropdown
    # from suggesting something that can only fail.
    parent_candidates = (
        Account.query.filter(Account.parent_account_id.is_(None), Account.id != account.id)
        .order_by(Account.name)
        .all()
    )
    return render_template(
        "account_detail.html",
        account=account,
        events=events,
        today=today,
        month_start=today.replace(day=1),
        fuel_types=fuel_types,
        tanks=tanks,
        bank_accounts=bank_accounts,
        aging=credit_aging(account, today) if account.balance > 0 else None,
        children=children,
        group_balance=account.group_balance,
        parent_candidates=parent_candidates,
    )


@app.route("/accounts/<int:account_id>/edit", methods=["POST"])
@login_required
@owner_required
def account_edit(account_id):
    account = db.session.get(Account, account_id) or abort(404)
    name = request.form.get("name", "").strip()
    phone = request.form.get("phone", "").strip()
    account_type = request.form.get("account_type", "")
    # Blank = top-level, so clearing the picker un-links a sub-account
    # again through this same path.
    parent_account_id = _parent_id_from_form(request.form)
    parent_error = _validate_parent(account, parent_account_id)
    # Free-text context (see Account.notes). Normalised to None when blank
    # so "no note" is one value in the database rather than two - the
    # Accounts list's note indicator tests truthiness, and an account
    # whose note was cleared must stop showing one.
    notes = request.form.get("notes", "").strip()

    if not name:
        flash("Please enter a name.", "error")
    elif account_type not in ACCOUNT_TYPES:
        flash("Please choose an account type.", "error")
    elif parent_error:
        flash(parent_error, "error")
    else:
        account.name = name
        account.phone = phone or None
        account.account_type = account_type
        account.parent_account_id = parent_account_id
        account.notes = notes or None
        db.session.commit()
        flash("Account details updated.", "success")

    return redirect(url_for("account_detail", account_id=account.id))


@app.route("/accounts/<int:account_id>/opening-balance", methods=["POST"])
@login_required
@owner_required
def account_opening_balance(account_id):
    account = db.session.get(Account, account_id) or abort(404)
    opening_balance = request.form.get("opening_balance", type=float)
    raw_date = request.form.get("opening_balance_date", "").strip()

    if opening_balance is None:
        flash("Please enter an opening balance (use 0 to clear it).", "error")
    elif opening_balance and not raw_date:
        flash("Please choose an as-of date for the opening balance.", "error")
    else:
        account.opening_balance = opening_balance
        account.opening_balance_date = parse_date_param(raw_date) if raw_date else None
        db.session.commit()
        flash("Opening balance updated.", "success")

    return redirect(url_for("account_detail", account_id=account.id))


@app.route("/accounts/<int:account_id>/delete", methods=["POST"])
@login_required
@owner_required
def account_delete(account_id):
    """Only allowed for an account with no transaction history and no
    opening balance - a genuine "made this by mistake" case. An account
    that's actually been used has to stay for the numbers to add up; you
    can still just stop using it."""
    account = db.session.get(Account, account_id) or abort(404)
    has_entries = (
        account.credit_entries
        or account.receipts
        or account.stock_purchases
        or account.supplier_payments
        or account.employee_loans
    )

    if account.children:
        flash(f"Can't delete {account.name} - it still has sub-accounts. Move them out first.", "error")
    elif has_entries or account.opening_balance:
        flash(f"Can't delete {account.name} - it already has transaction history or a nonzero opening balance.", "error")
    else:
        name = account.name
        db.session.delete(account)
        db.session.commit()
        flash(f'Deleted "{name}".', "success")

    return redirect(url_for("accounts"))


@app.route("/accounts/entry/credit/<int:entry_id>/edit", methods=["POST"])
@login_required
@owner_required
def account_entry_credit_edit(entry_id):
    """Same entry_mode toggle as ledger_credit() (see its docstring) - lets
    an existing credit-given entry be switched between liters-mode and
    amount-mode, or stay in either, on save."""
    entry = db.session.get(CreditGiven, entry_id) or abort(404)
    # Honours an optional hidden "next" field exactly as entry_delete()
    # does, so this one editor serves both the account page (which posts
    # no "next" and so falls back to itself) and the Ledger feed.
    next_url = request.form.get("next") or url_for("account_detail", account_id=entry.account_id)
    entry_date = parse_date_param(request.form.get("entry_date"))
    fuel_type_id = request.form.get("fuel_type_id", type=int)
    entry_mode = _resolve_entry_mode(request.form)
    liters_in = request.form.get("liters", type=float)
    amount_in = request.form.get("amount", type=float)
    vehicle_number = request.form.get("vehicle_number", "").strip()
    note = request.form.get("note", "").strip()
    fuel = db.session.get(FuelType, fuel_type_id) if fuel_type_id else None

    if not fuel:
        flash("Please choose a valid fuel type.", "error")
    elif (amount_error := _credit_amount_error(fuel, entry_date, entry_mode, liters_in, amount_in)):
        flash(amount_error, "error")
    else:
        price = price_on_date(fuel, entry_date)
        liters, amount = _derive_credit_liters_amount(entry_mode, liters_in, amount_in, price)
        entry.entry_date = entry_date
        entry.fuel_type_id = fuel.id
        entry.liters = liters
        entry.price_per_liter = price
        entry.amount = amount
        entry.vehicle_number = vehicle_number or None
        entry.note = note or None
        db.session.commit()
        flash("Credit entry updated.", "success")

    return redirect(next_url)


@app.route("/accounts/entry/receipt/<int:entry_id>/edit", methods=["POST"])
@login_required
@owner_required
def account_entry_receipt_edit(entry_id):
    entry = db.session.get(Receipt, entry_id) or abort(404)
    # Honours an optional hidden "next" field exactly as entry_delete()
    # does, so this one editor serves both the account page (which posts
    # no "next" and so falls back to itself) and the Ledger feed.
    next_url = request.form.get("next") or url_for("account_detail", account_id=entry.account_id)
    entry_date = parse_date_param(request.form.get("entry_date"))
    amount = request.form.get("amount", type=float)
    note = request.form.get("note", "").strip()
    method, bank_account, method_error = resolve_payment_method(request.form)

    if not amount or amount <= 0:
        flash("Amount must be a positive number.", "error")
    elif method_error:
        flash(method_error, "error")
    else:
        entry.entry_date = entry_date
        entry.amount = amount
        entry.method = method
        entry.bank_account_id = bank_account.id if bank_account else None
        entry.note = note or None
        db.session.commit()
        flash("Receipt updated.", "success")

    return redirect(next_url)


@app.route("/accounts/entry/purchase/<int:entry_id>/edit", methods=["POST"])
@login_required
@owner_required
def account_entry_purchase_edit(entry_id):
    entry = db.session.get(StockPurchase, entry_id) or abort(404)
    # Honours an optional hidden "next" field exactly as entry_delete()
    # does, so this one editor serves both the account page (which posts
    # no "next" and so falls back to itself) and the Ledger feed.
    next_url = request.form.get("next") or url_for("account_detail", account_id=entry.account_id)
    entry_date = parse_date_param(request.form.get("entry_date"))
    tank_id = request.form.get("tank_id", type=int)
    liters = request.form.get("liters", type=float)
    cost = request.form.get("cost", type=float)
    note = request.form.get("note", "").strip()
    tank = db.session.get(Tank, tank_id) if tank_id else None

    if not tank:
        flash("Please choose a valid tank.", "error")
    elif not liters or liters <= 0:
        flash("Liters must be a positive number.", "error")
    elif not cost or cost <= 0:
        flash("Cost must be a positive number.", "error")
    else:
        entry.entry_date = entry_date
        entry.tank_id = tank.id
        entry.liters = liters
        entry.cost = cost
        entry.note = note or None
        db.session.commit()
        flash("Purchase updated.", "success")

    return redirect(next_url)


@app.route("/accounts/entry/supplier-payment/<int:entry_id>/edit", methods=["POST"])
@login_required
@owner_required
def account_entry_supplier_payment_edit(entry_id):
    entry = db.session.get(SupplierPayment, entry_id) or abort(404)
    # Honours an optional hidden "next" field exactly as entry_delete()
    # does, so this one editor serves both the account page (which posts
    # no "next" and so falls back to itself) and the Ledger feed.
    next_url = request.form.get("next") or url_for("account_detail", account_id=entry.account_id)
    entry_date = parse_date_param(request.form.get("entry_date"))
    amount = request.form.get("amount", type=float)
    note = request.form.get("note", "").strip()
    method, bank_account, error = _validate_cash_payment_edit(entry, entry_date, amount, request.form)

    if error:
        flash(error, "error")
    else:
        entry.entry_date = entry_date
        entry.amount = amount
        entry.method = method
        entry.bank_account_id = bank_account.id if bank_account else None
        entry.note = note or None
        db.session.commit()
        flash("Payment updated.", "success")

    return redirect(next_url)


@app.route("/accounts/entry/employee-loan/<int:entry_id>/edit", methods=["POST"])
@login_required
@owner_required
def account_entry_employee_loan_edit(entry_id):
    entry = db.session.get(EmployeeLoan, entry_id) or abort(404)
    # Honours an optional hidden "next" field exactly as entry_delete()
    # does, so this one editor serves both the account page (which posts
    # no "next" and so falls back to itself) and the Ledger feed.
    next_url = request.form.get("next") or url_for("account_detail", account_id=entry.account_id)
    entry_date = parse_date_param(request.form.get("entry_date"))
    amount = request.form.get("amount", type=float)
    note = request.form.get("note", "").strip()
    method, bank_account, error = _validate_cash_payment_edit(entry, entry_date, amount, request.form)

    if error:
        flash(error, "error")
    else:
        entry.entry_date = entry_date
        entry.amount = amount
        entry.method = method
        entry.bank_account_id = bank_account.id if bank_account else None
        entry.note = note or None
        db.session.commit()
        flash("Loan updated.", "success")

    return redirect(next_url)


@app.route("/accounts/entry/salary/<int:entry_id>/edit", methods=["POST"])
@login_required
@owner_required
def account_entry_salary_edit(entry_id):
    entry = db.session.get(SalaryPayment, entry_id) or abort(404)
    entry_date = parse_date_param(request.form.get("entry_date"))
    gross = request.form.get("gross_amount", type=float)
    deduction = request.form.get("deduction_amount", type=float) or 0
    period_label = request.form.get("period_label", "").strip()
    note = request.form.get("note", "").strip()
    method, bank_account, method_error = resolve_payment_method(request.form)
    next_url = request.form.get("next") or url_for("account_detail", account_id=entry.account_id)

    old_cash_net = entry.net_paid if entry.method == "cash" else 0
    new_net = round((gross or 0) - deduction, 2)
    new_cash_net = new_net if method == "cash" else 0
    # What this account would owe with THIS entry's deduction backed out,
    # so the new deduction is checked against the right ceiling.
    outstanding_without_entry = round(entry.account.balance + entry.deduction_amount, 2)

    if not gross or gross <= 0:
        flash("Salary amount must be a positive number.", "error")
    elif deduction < 0:
        flash("Deduction can't be negative.", "error")
    elif deduction > gross:
        flash("Deduction can't be more than the salary itself.", "error")
    elif deduction > outstanding_without_entry + 0.01:
        flash(
            f"{entry.account.name} only owes Rs {format_number(max(outstanding_without_entry, 0))}, so you "
            f"can't deduct Rs {format_number(deduction)}.",
            "error",
        )
    elif method_error:
        flash(method_error, "error")
    elif would_overdraw_cash(new_cash_net, entry_date, old_cash_net, entry.entry_date):
        flash(cash_shortfall_message(entry_date), "error")
    else:
        entry.entry_date = entry_date
        entry.gross_amount = gross
        entry.deduction_amount = deduction
        entry.period_label = period_label or None
        entry.method = method
        entry.bank_account_id = bank_account.id if bank_account else None
        entry.note = note or None
        db.session.commit()
        flash("Salary payment updated.", "success")

    return redirect(next_url)


def _account_statement_context(account, from_date, to_date):
    """The balance carried into a date range, every entry inside it, and
    the closing balance - all derived from the same account_ledger_events()
    the account detail page uses. Shared by the Statement page and its
    PDF/Excel export."""
    all_events = list(reversed(account_ledger_events(account)))  # oldest first
    before = [e for e in all_events if e["entry_date"] < from_date]
    inside = [e for e in all_events if from_date <= e["entry_date"] <= to_date]
    # sum() starts from 0.0, not the default 0 - a brand-new account (or a
    # range with nothing before it) would otherwise leave `opening` as a
    # bare int, which the PDF/Excel export would then render as "0"
    # instead of "0.00" alongside every other money figure.
    opening = round(sum((e["delta"] for e in before), 0.0), 2)

    return {
        "opening": opening,
        "events": inside,
        "closing": inside[-1]["running_balance"] if inside else opening,
        "aging": credit_aging(account, to_date),
    }


@app.route("/accounts/<int:account_id>/statement")
@login_required
def account_statement(account_id):
    """A print-friendly statement for a date range - the thing you actually
    hand or send to a credit customer."""
    account = db.session.get(Account, account_id) or abort(404)
    today = date.today()
    from_date = parse_date_param(request.args.get("from"), fallback=today.replace(day=1))
    to_date = parse_date_param(request.args.get("to"), fallback=today)
    ctx = _account_statement_context(account, from_date, to_date)

    return render_template(
        "account_statement.html",
        account=account,
        from_date=from_date,
        to_date=to_date,
        today=today,
        **ctx,
    )


_STATEMENT_KIND_LABELS = {
    "opening": "Opening Balance",
    "credit": "Fuel on Credit",
    "receipt": "Payment Received",
    "purchase": "Purchase (Credit)",
    "supplier_payment": "Payment Made",
    "employee_loan": "Loan / Advance",
    "salary": "Salary",
    "sales_return": "Sales Return",
    "product_sale": "Non-Fuel Sale",
    "product_purchase": "Product Purchase (Credit)",
    "other_income": "Other Income",
}


def _statement_kind_label(e):
    """Same "Type" column text account_statement.html shows for one event
    (see its kind if/elif chain), reused so the export doesn't drift from
    the page - mirrors _statement_event_details() below for the Details
    column. employee_loan is the one kind whose label isn't a static
    per-kind string: it reads "Owner Drawing" or "Loan / Advance"
    depending on the row's own EmployeeLoan.kind (see models.py)."""
    if e["kind"] == "employee_loan":
        return "Owner Drawing" if e["obj"].kind == "drawing" else "Loan / Advance"
    return _STATEMENT_KIND_LABELS.get(e["kind"], e["kind"])


def _statement_event_details(e):
    """Same "Details" column text the account_statement.html table shows
    for one event, reused so the export doesn't drift from the page."""
    obj = e["obj"]
    if e["kind"] == "credit":
        bits = [f"{format_number(obj.liters)} L {obj.fuel_type.name}"]
        if obj.vehicle_number:
            bits.append(obj.vehicle_number)
        if obj.note:
            bits.append(obj.note)
        return " - ".join(bits)
    if e["kind"] == "purchase":
        bits = [f"{format_number(obj.liters)} L {obj.tank.label}"]
        if obj.note:
            bits.append(obj.note)
        return " - ".join(bits)
    if e["kind"] == "sales_return":
        bits = [f"{format_number(obj.liters)} L {obj.fuel_type.name} returned to {obj.tank.label}"]
        if obj.note:
            bits.append(obj.note)
        return " - ".join(bits)
    if e["kind"] in ("product_sale", "product_purchase"):
        bits = [f"{format_number(obj.quantity)} {obj.product.unit} {obj.product.label}"]
        if obj.note:
            bits.append(obj.note)
        return " - ".join(bits)
    if e["kind"] == "salary":
        text = obj.period_label or "Salary"
        if obj.deduction_amount:
            text += f" - Rs {format_number(obj.deduction_amount)} deducted against advance"
        return text
    if e["kind"] == "other_income":
        # An Other Income row only ever reaches an account's statement when
        # method == "credit" - account_id is never set otherwise - so
        # there's no payment-method text to show here, just the
        # description, mirroring how product_sale/product_purchase's
        # branch above doesn't show payment method either.
        return obj.description
    if obj is not None:
        text = f"Via {obj.bank_account.name}" if obj.method == "bank" else "Cash"
        if obj.note:
            text += f" - {obj.note}"
        return text
    return "-"


@app.route("/accounts/<int:account_id>/statement/export")
@login_required
def account_statement_export(account_id):
    """Mirrors account_statement() - same account, same date range - as a
    PDF or Excel download instead of a page."""
    fmt = _resolve_export_format()
    account = db.session.get(Account, account_id) or abort(404)
    today = date.today()
    from_date = parse_date_param(request.args.get("from"), fallback=today.replace(day=1))
    to_date = parse_date_param(request.args.get("to"), fallback=today)
    ctx = _account_statement_context(account, from_date, to_date)

    statement_rows = [
        [from_date.isoformat(), "Brought Forward", "Balance carried into this period", "", "", ctx["opening"]]
    ]
    for e in ctx["events"]:
        statement_rows.append(
            [
                e["entry_date"].isoformat(),
                _statement_kind_label(e),
                _statement_event_details(e),
                e["delta"] if e["delta"] > 0 else "",
                -e["delta"] if e["delta"] < 0 else "",
                e["running_balance"],
            ]
        )
    statement_rows.append(
        ["", "", f"Closing balance as of {to_date.isoformat()}", "", "", ctx["closing"]]
    )

    blocks = [
        {
            "type": "summary",
            "rows": [
                ("Account", account.name),
                ("Type", account.account_type.capitalize()),
                ("Phone", account.phone or "-"),
                ("Period", f"{from_date.isoformat()} to {to_date.isoformat()}"),
            ],
        },
        {
            "type": "table",
            "heading": "Statement",
            "columns": ["Date", "Type", "Details", "Debit (Rs)", "Credit (Rs)", "Balance (Rs)"],
            "rows": statement_rows,
            "align": ["left", "left", "left", "right", "right", "right"],
        },
    ]
    if ctx["aging"] and ctx["aging"]["outstanding"]:
        blocks.append(
            {
                "type": "table",
                "heading": "Age of Outstanding Amount",
                "columns": ["0-30 days", "31-60 days", "61-90 days", "Over 90 days"],
                "rows": [
                    [
                        ctx["aging"]["buckets"]["0-30"],
                        ctx["aging"]["buckets"]["31-60"],
                        ctx["aging"]["buckets"]["61-90"],
                        ctx["aging"]["buckets"]["90+"],
                    ]
                ],
                "align": ["right", "right", "right", "right"],
            }
        )

    return _send_export(
        fmt,
        pdf_title=f"Statement - {account.name}",
        pdf_subtitle=f"{from_date.strftime('%d %b %Y')} to {to_date.strftime('%d %b %Y')}",
        xlsx_sheet_name="Statement",
        blocks=blocks,
        filename_base=f"petrol-khata-statement-{slugify(account.name)}-{date.today().isoformat()}",
    )


@app.route("/accounts/bank/<int:bank_account_id>")
@login_required
def bank_account_detail(bank_account_id):
    bank_account = db.session.get(BankAccount, bank_account_id) or abort(404)
    events = bank_account_ledger_events(bank_account)
    bank_accounts = BankAccount.query.order_by(BankAccount.name).all()
    tanks = Tank.query.order_by(Tank.number).all()
    return render_template(
        "bank_account_detail.html",
        bank_account=bank_account,
        events=events,
        today=date.today(),
        bank_accounts=bank_accounts,
        tanks=tanks,
    )


@app.route("/accounts/bank/<int:bank_account_id>/edit", methods=["POST"])
@login_required
@owner_required
def bank_account_edit(bank_account_id):
    bank_account = db.session.get(BankAccount, bank_account_id) or abort(404)
    name = request.form.get("name", "").strip()

    if not name:
        flash("Please enter a name.", "error")
    else:
        bank_account.name = name
        db.session.commit()
        flash("Bank account details updated.", "success")

    return redirect(url_for("bank_account_detail", bank_account_id=bank_account.id))


@app.route("/accounts/bank/<int:bank_account_id>/opening-balance", methods=["POST"])
@login_required
@owner_required
def bank_account_opening_balance(bank_account_id):
    bank_account = db.session.get(BankAccount, bank_account_id) or abort(404)
    opening_balance = request.form.get("opening_balance", type=float)
    raw_date = request.form.get("opening_balance_date", "").strip()

    if opening_balance is None:
        flash("Please enter an opening balance (use 0 to clear it).", "error")
    elif opening_balance and not raw_date:
        flash("Please choose an as-of date for the opening balance.", "error")
    else:
        bank_account.opening_balance = opening_balance
        bank_account.opening_balance_date = parse_date_param(raw_date) if raw_date else None
        db.session.commit()
        flash("Opening balance updated.", "success")

    return redirect(url_for("bank_account_detail", bank_account_id=bank_account.id))


@app.route("/accounts/bank/<int:bank_account_id>/delete", methods=["POST"])
@login_required
@owner_required
def bank_account_delete(bank_account_id):
    """Only allowed for a bank account with no transaction history and no
    opening balance - see account_delete for the same reasoning."""
    bank_account = db.session.get(BankAccount, bank_account_id) or abort(404)
    has_entries = (
        bank_account.bank_sales
        or bank_account.deposits
        or bank_account.receipts
        or bank_account.employee_loans_paid
        or bank_account.expenses
        or bank_account.fuel_purchases
        or bank_account.supplier_payments_paid
    )

    if has_entries or bank_account.opening_balance:
        flash(
            f"Can't delete {bank_account.name} - it already has transaction history or a nonzero opening balance.",
            "error",
        )
    else:
        name = bank_account.name
        db.session.delete(bank_account)
        db.session.commit()
        flash(f'Deleted "{name}".', "success")

    return redirect(url_for("accounts"))


@app.route("/accounts/entry/bank-sale/<int:entry_id>/edit", methods=["POST"])
@login_required
@owner_required
def account_entry_bank_sale_edit(entry_id):
    entry = db.session.get(BankSale, entry_id) or abort(404)
    entry_date = parse_date_param(request.form.get("entry_date"))
    amount = request.form.get("amount", type=float)
    note = request.form.get("note", "").strip()
    error = _validate_amount_cash_edit(entry, entry_date, amount)

    if error:
        flash(error, "error")
    else:
        entry.entry_date = entry_date
        entry.amount = amount
        entry.note = note or None
        db.session.commit()
        flash("Bank sale updated.", "success")

    return redirect(url_for("bank_account_detail", bank_account_id=entry.bank_account_id))


@app.route("/accounts/entry/cash-deposit/<int:entry_id>/edit", methods=["POST"])
@login_required
@owner_required
def account_entry_cash_deposit_edit(entry_id):
    entry = db.session.get(CashDeposit, entry_id) or abort(404)
    entry_date = parse_date_param(request.form.get("entry_date"))
    amount = request.form.get("amount", type=float)
    note = request.form.get("note", "").strip()
    error = _validate_amount_cash_edit(entry, entry_date, amount)

    if error:
        flash(error, "error")
    else:
        entry.entry_date = entry_date
        entry.amount = amount
        entry.note = note or None
        db.session.commit()
        flash("Cash deposit updated.", "success")

    return redirect(url_for("bank_account_detail", bank_account_id=entry.bank_account_id))


@app.route("/accounts/entry/expense/<int:entry_id>/edit", methods=["POST"])
@login_required
@owner_required
def account_entry_expense_edit(entry_id):
    """Shared expense editor, reachable from the Accounts page's all-time
    Expenses list, cash-in-hand's page, or a bank account's page -
    wherever the entry happens to be shown - redirecting back to
    whichever of those pages linked here via the hidden "next" field."""
    entry = db.session.get(Expense, entry_id) or abort(404)
    entry_date = parse_date_param(request.form.get("entry_date"))
    category = request.form.get("category", "").strip()
    description = request.form.get("description", "").strip()
    amount = request.form.get("amount", type=float)
    method, bank_account, method_error = resolve_payment_method(request.form)
    next_url = request.form.get("next") or url_for("accounts")
    old_cash_amount = entry.amount if entry.method == "cash" else 0
    new_cash_amount = amount if (amount and method == "cash") else 0

    if not category:
        flash("Please enter an expense category.", "error")
    elif not amount or amount <= 0:
        flash("Amount must be a positive number.", "error")
    elif method_error:
        flash(method_error, "error")
    elif would_overdraw_cash(new_cash_amount, entry_date, old_cash_amount, entry.entry_date):
        flash(cash_shortfall_message(entry_date), "error")
    else:
        entry.entry_date = entry_date
        entry.category = category
        entry.description = description or None
        entry.amount = amount
        entry.method = method
        entry.bank_account_id = bank_account.id if bank_account else None
        db.session.commit()
        flash("Expense updated.", "success")

    return redirect(next_url)


@app.route("/accounts/entry/fuel-purchase/<int:entry_id>/edit", methods=["POST"])
@login_required
@owner_required
def account_entry_fuel_purchase_edit(entry_id):
    """Editor for a cash-paid fuel purchase (payment_type == "cash"),
    reachable from cash-in-hand's page or the bank account it was paid
    from - redirects back via the hidden "next" field. Credit purchases
    are edited from the supplier account's page instead
    (account_entry_purchase_edit) since payment_type can't be changed
    here."""
    entry = db.session.get(StockPurchase, entry_id) or abort(404)
    entry_date = parse_date_param(request.form.get("entry_date"))
    tank_id = request.form.get("tank_id", type=int)
    liters = request.form.get("liters", type=float)
    cost = request.form.get("cost", type=float)
    note = request.form.get("note", "").strip()
    method, bank_account, method_error = resolve_payment_method(request.form)
    next_url = request.form.get("next") or url_for("accounts")
    tank = db.session.get(Tank, tank_id) if tank_id else None
    old_cash_amount = (entry.cost or 0) if entry.method == "cash" else 0
    new_cash_amount = cost if method == "cash" else 0

    if not tank:
        flash("Please choose a valid tank.", "error")
    elif not liters or liters <= 0:
        flash("Liters must be a positive number.", "error")
    elif not cost or cost <= 0:
        flash("Cost must be a positive number.", "error")
    elif method_error:
        flash(method_error, "error")
    elif would_overdraw_cash(new_cash_amount, entry_date, old_cash_amount, entry.entry_date):
        flash(cash_shortfall_message(entry_date), "error")
    else:
        entry.entry_date = entry_date
        entry.tank_id = tank.id
        entry.liters = liters
        entry.cost = cost
        entry.method = method
        entry.bank_account_id = bank_account.id if bank_account else None
        entry.note = note or None
        db.session.commit()
        flash("Fuel purchase updated.", "success")

    return redirect(next_url)


@app.route("/accounts/cash")
@login_required
def cash_account_detail():
    cash_account = get_cash_account()
    events = cash_account_ledger_events(cash_account)
    bank_accounts = BankAccount.query.order_by(BankAccount.name).all()
    tanks = Tank.query.order_by(Tank.number).all()
    return render_template(
        "cash_account_detail.html",
        cash_account=cash_account,
        balance=cash_account_balance(cash_account),
        events=events,
        today=date.today(),
        bank_accounts=bank_accounts,
        tanks=tanks,
    )


@app.route("/accounts/cash/opening-balance", methods=["POST"])
@login_required
@owner_required
def cash_account_opening_balance():
    cash_account = get_cash_account()
    opening_balance = request.form.get("opening_balance", type=float)
    raw_date = request.form.get("opening_balance_date", "").strip()

    if opening_balance is None or opening_balance < 0:
        flash("Please enter a valid opening balance (0 or more - cash in hand can't be negative).", "error")
    elif opening_balance and not raw_date:
        flash("Please choose an as-of date for the opening balance.", "error")
    else:
        cash_account.opening_balance = opening_balance
        cash_account.opening_balance_date = parse_date_param(raw_date) if raw_date else None
        db.session.commit()
        flash("Opening balance updated.", "success")

    return redirect(url_for("cash_account_detail"))
