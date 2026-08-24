"""Reports routes: the daily reports page, its PDF/XLSX export, the
monthly report (and its export), and the trends page - plus the
month-scoped context builders and narrative helpers used only by them.

Moved verbatim out of app.py - this is the reports/... route group,
including monthly_narrative() and profit_walkthrough() (used only by the
monthly report) and their own severity-ordering constant, unchanged
except for their location.
"""

from datetime import date, timedelta

from flask import (
    render_template,
    request,
    url_for,
)
from flask_login import login_required
from sqlalchemy import func

import charts
from formatting import format_number
from extensions import db
from ledger_logic import (
    account_positions,
    attendant_variance_summary,
    bank_account_balance_as_of,
    best_sales_day_for_period,
    book_stock,
    cash_account_balance_as_of,
    cash_movement_for_period,
    cogs_for_period,
    credit_discounts_for_period,
    _group_sum_by_day,
    fuel_sales_for_date,
    handover_rows_for_date,
    payables_schedule,
    product_margin_for_period,
    stock_purchases_by_fuel_for_period,
    stock_series,
    weighted_avg_cost,
    working_capital,
    MONTHLY_SHORTFALL_TOLERANCE,
    THIN_MARGIN_PER_LITER,
)
from models import (
    Account,
    BankAccount,
    BankSale,
    CreditGiven,
    DirectSale,
    Expense,
    FuelType,
    Nozzle,
    OtherIncome,
    ProductPurchase,
    ProductSale,
    Receipt,
    SalaryPayment,
    Sale,
    SalesReturn,
    StockPurchase,
    SupplierPayment,
    Tank,
    TankDip,
    TankerDeal,
)
from app import (
    app,
    _resolve_export_format,
    _send_export,
    get_cash_account,
    owner_required,
    parse_date_param,
)

_NARRATIVE_SEVERITY_ORDER = {"critical": 0, "warning": 1, "good": 2, "info": 3}


def monthly_narrative(start, end, ctx, prior_ctx, prior_month_label, best_day):
    """Rule-based, deterministic observations about one month - the
    Monthly Report's counterpart to attention_items(), same return shape
    (severity/title/detail/url) so it reuses the Dashboard's
    .attention-list markup verbatim. Every number quoted comes from
    ctx/prior_ctx/best_day, which the caller has already computed; this
    function issues ZERO queries of its own. Adds one severity value
    attention_items() doesn't have - "good" - because unlike "Needs
    Attention" this list is also meant to say what went right. Thresholds
    come from the named constants in ledger_logic.py."""
    items = []
    detail = ctx["cogs_detail"]

    def mpl(r):
        return r["margin"] / r["liters"] if r["liters"] else 0.0

    # R1 - loss-making month (critical).
    if ctx["net_profit"] < 0:
        items.append(
            {
                "severity": "critical",
                "title": "The month closed at a loss",
                "detail": (
                    f"Net profit for {start.strftime('%B %Y')} is Rs {format_number(ctx['net_profit'])}. "
                    f"Total gross margin Rs {format_number(ctx['total_gross_margin'])}, tanker deal margin "
                    f"Rs {format_number(ctx['tanker_margin'])} and other income "
                    f"Rs {format_number(ctx['other_income_total'])} did not cover Rs {format_number(ctx['expenses_total'])} "
                    f"of expenses and Rs {format_number(ctx['salaries_total'])} of salaries."
                ),
                "url": None,
            }
        )

    # R2 - a fuel sold below cost (critical).
    for r in detail:
        if r["liters"] > 0 and mpl(r) < 0:
            items.append(
                {
                    "severity": "critical",
                    "title": f"{r['fuel']}: sold below cost this month",
                    "detail": (
                        f"Rs {format_number(mpl(r))} margin per litre across {format_number(r['liters'])} L - net revenue "
                        f"Rs {format_number(r['revenue'])} against Rs {format_number(r['cost'])} of cost at the "
                        "weighted-average purchase price."
                    ),
                    "url": None,
                }
            )

    # R3 - thin margin (warning).
    for r in detail:
        if r["liters"] > 0 and 0 <= mpl(r) < THIN_MARGIN_PER_LITER:
            items.append(
                {
                    "severity": "warning",
                    "title": f"{r['fuel']}: thin margin",
                    "detail": (
                        f"Only Rs {format_number(mpl(r))} margin per litre this month, under the "
                        f"Rs {format_number(THIN_MARGIN_PER_LITER)}/L mark - Rs {format_number(r['margin'])} earned on "
                        f"{format_number(r['liters'])} L."
                    ),
                    "url": None,
                }
            )

    # R4 - profit against the prior full month (good/warning/info).
    if prior_ctx["net_profit"] != 0:
        delta = round(ctx["net_profit"] - prior_ctx["net_profit"], 2)
        pct = round(delta / abs(prior_ctx["net_profit"]) * 100, 1)
        if delta >= 0:
            items.append(
                {
                    "severity": "good",
                    "title": "Profit is up on last month",
                    "detail": (
                        f"Rs {format_number(ctx['net_profit'])} this month against Rs {format_number(prior_ctx['net_profit'])} "
                        f"in {prior_month_label} - up Rs {format_number(delta)} ({pct:+.1f}%)."
                    ),
                    "url": None,
                }
            )
        else:
            items.append(
                {
                    "severity": "warning",
                    "title": "Profit is down on last month",
                    "detail": (
                        f"Rs {format_number(ctx['net_profit'])} this month against Rs {format_number(prior_ctx['net_profit'])} "
                        f"in {prior_month_label} - down Rs {format_number(abs(delta))} ({pct:+.1f}%)."
                    ),
                    "url": None,
                }
            )
    else:
        items.append(
            {
                "severity": "info",
                "title": "No comparable prior month",
                "detail": f"{prior_month_label} recorded no net profit figure to compare against.",
                "url": None,
            }
        )

    # R5 - best fuel by revenue (good).
    total_net_fuel_revenue = sum(r["revenue"] for r in detail)
    if detail and total_net_fuel_revenue > 0:
        top = max(detail, key=lambda r: r["revenue"])
        share = top["revenue"] / total_net_fuel_revenue * 100
        items.append(
            {
                "severity": "good",
                "title": f"{top['fuel']} led the month",
                "detail": (
                    f"{format_number(top['liters'])} L sold for Rs {format_number(top['revenue'])} - {share:.1f}% of net "
                    f"fuel revenue, at Rs {format_number(mpl(top))} margin per litre."
                ),
                "url": None,
            }
        )

    # R6 - best single sales day (good).
    if best_day is not None:
        items.append(
            {
                "severity": "good",
                "title": f"Best day: {best_day['date'].strftime('%d %b %Y')}",
                "detail": f"Rs {format_number(best_day['amount'])} of fuel sold that day, the highest of the month.",
                "url": url_for("reports", date=best_day["date"].isoformat()),
            }
        )

    # R7 - attendant cash shortfall (warning). Only the single worst
    # attendant is reported - the full table is right below on the page.
    if ctx["attendant_variances"]:
        w = ctx["attendant_variances"][0]
        if w["total_variance"] < -MONTHLY_SHORTFALL_TOLERANCE:
            items.append(
                {
                    "severity": "warning",
                    "title": f"{w['name']}: cash short this month",
                    "detail": (
                        f"Rs {format_number(abs(w['total_variance']))} net short across {w['shifts']} reconciled "
                        f"shift(s), {w['shortfalls']} of them short."
                    ),
                    "url": url_for("account_detail", account_id=w["account"].id) if w["account"] else None,
                }
            )

    # R8 - stock cover vs sales (info).
    if ctx["purchases_liters"] > 0 or ctx["liters_sold"] > 0:
        if ctx["purchases_liters"] < ctx["liters_sold"]:
            items.append(
                {
                    "severity": "info",
                    "title": "Sold more than was received",
                    "detail": (
                        f"{format_number(ctx['liters_sold'])} L sold against {format_number(ctx['purchases_liters'])} L "
                        "received - the difference came out of tank stock."
                    ),
                    "url": url_for("inventory"),
                }
            )
        else:
            items.append(
                {
                    "severity": "info",
                    "title": "Received more than was sold",
                    "detail": (
                        f"{format_number(ctx['purchases_liters'])} L received against {format_number(ctx['liters_sold'])} L "
                        "sold - the difference is still in the tanks."
                    ),
                    "url": url_for("inventory"),
                }
            )

    items.sort(key=lambda i: _NARRATIVE_SEVERITY_ORDER[i["severity"]])
    return items


def profit_walkthrough(ctx):
    """The income-statement figures already in ctx, re-expressed as an
    ordered walk with a running total, so the page can show HOW net
    profit was reached instead of a flat row of cards. Adds no arithmetic
    of its own beyond the running total - every `amount` is a ctx value
    read verbatim, and the walk's final `running` MUST equal
    ctx["net_profit"] to 2dp; if it ever doesn't,
    _reports_monthly_context()'s own sales-returns double-count invariant
    (see its docstring) has been broken. Deliberately assertion-free (a
    report must render, not 500) - the equality above is restated here as
    the thing to check, not enforced with an assert."""
    r = ctx
    rows = []
    running = 0.0

    def start_row(label, amount, note):
        nonlocal running
        running = amount
        rows.append({"kind": "start", "label": label, "amount": amount, "running": running, "note": note})

    def memo_row(label, amount, note):
        rows.append({"kind": "memo", "label": label, "amount": amount, "running": None, "note": note})

    def less_row(label, amount, note):
        nonlocal running
        running = round(running - amount, 2)
        rows.append({"kind": "less", "label": label, "amount": amount, "running": running, "note": note})

    def add_row(label, amount, note):
        nonlocal running
        running = round(running + amount, 2)
        rows.append({"kind": "add", "label": label, "amount": amount, "running": running, "note": note})

    def subtotal_row(label, amount, note):
        rows.append({"kind": "subtotal", "label": label, "amount": amount, "running": running, "note": note})

    def total_row(label, amount, note):
        rows.append({"kind": "total", "label": label, "amount": amount, "running": amount, "note": note})

    start_row(
        "Fuel Revenue, Gross", r["revenue"],
        f"{format_number(r['liters_sold'])} L sold · {format_number(r['testing_liters'])} L testing",
    )
    memo_row(
        "Discounts already netted out above", r["total_discounts"],
        "Credit sales billed below list price - subtracted from Fuel Revenue, Gross, not again below.",
    )
    less_row(
        "Sales Returns", r["sales_returns_amount"],
        f"{format_number(r['sales_returns_liters'])} L returned, any refund method",
    )
    subtotal_row("Net Fuel Revenue", r["net_revenue"], "Gross revenue less sales returns")
    less_row("Cost of Fuel Sold", r["cogs"], "Net litres sold × weighted-average purchase cost")
    subtotal_row(
        "Fuel Gross Margin", r["gross_margin"],
        f"{(r['gross_margin'] / r['net_revenue'] * 100) if r['net_revenue'] else 0:.1f}% of net fuel revenue",
    )
    memo_row("Product Revenue", r["product_revenue"], "")
    memo_row(
        "Less: Cost of Products Sold", r["product_cost"],
        "Exact per line - the rate snapshotted at the moment of that sale, not a weighted average",
    )
    add_row(
        "Product Gross Margin", r["product_commission"],
        f"{(r['product_commission'] / r['product_revenue'] * 100) if r['product_revenue'] else 0:.1f}% of product revenue",
    )
    subtotal_row("Total Gross Margin", r["total_gross_margin"], "Fuel + product gross margin")
    add_row(
        "Tanker Deal Margin", r["tanker_margin"],
        f"Pass-through deals - Rs {format_number(r['tanker_revenue'])} sold against "
        f"Rs {format_number(r['tanker_cost'])} cost on {format_number(r['tanker_liters'])} L. "
        "No tank stock involved: this fuel went straight from the supplier to the "
        "customer, so it is not in Fuel Revenue or Cost of Fuel Sold above.",
    )
    add_row(
        "Other Income", r["other_income_total"],
        "Rent, side-business share and similar - no associated cost",
    )
    less_row("Expenses", r["expenses_total"], "")
    less_row("Salaries", r["salaries_total"], "Full salary earned, before deductions")
    total_row(
        "Net Profit", r["net_profit"],
        "Total gross margin + tanker deal margin + other income − expenses − salaries",
    )
    return rows


# -------------------------------------------------------------- reports ---

def _reports_context(selected_date):
    """Every figure the Daily Report shows, for one date - shared by the
    HTML page and its PDF/Excel export so the two can never drift apart."""
    sales = (
        Sale.query.filter_by(entry_date=selected_date)
        .join(Nozzle)
        .order_by(Nozzle.dispenser_id, Nozzle.nozzle_number)
        .all()
    )
    # Direct-entry tank-level sales for the same date - see DirectSale's
    # docstring in models.py. There is no per-nozzle breakdown table for
    # these (they're not in the returned context below, only folded into
    # the same totals Sale contributes to), since DirectSale has no
    # nozzle to break down by in the first place.
    direct_sales = DirectSale.query.filter_by(entry_date=selected_date).all()
    # Every sum() below starts from 0.0 rather than the default 0 - on a
    # date with none of that kind, plain sum(empty) is an int, which the
    # HTML template's "%.2f" format papers over but the PDF export's
    # formatter (which has to tell a count apart from a money figure by
    # its Python type) would otherwise render as a bare "0" instead of
    # "0.00", inconsistent with every other row.
    total_sales = sum((s.total_amount for s in sales), 0.0) + sum((d.total_amount for d in direct_sales), 0.0)
    # Sale/DirectSale always record fuel at full list price with no
    # discount capability of their own - net out any discretionary discount
    # given via a CreditGiven row on this date (see
    # credit_discounts_for_period()'s docstring), or total_sales (and
    # everything below derived from it - cash_sales, net_cash_flow) would
    # overstate revenue/cash by the discount.
    total_discounts = credit_discounts_for_period(selected_date, selected_date)
    total_sales -= total_discounts
    total_liters = sum((s.liters for s in sales), 0.0) + sum((d.liters for d in direct_sales), 0.0)
    # Testing has no DirectSale equivalent at all (see models.py) - it
    # only ever comes from metered Sale rows.
    total_testing_liters = sum((s.testing_liters for s in sales), 0.0)
    by_fuel = fuel_sales_for_date(selected_date)
    # Testing isn't part of by_fuel (fuel_sales_for_date() means net sold,
    # same as everywhere else) - broken out per fuel here purely for this
    # table, since it's the only place that wants it split that way.
    testing_by_fuel = {}
    for s in sales:
        if s.testing_liters:
            name = s.nozzle.tank.fuel_type.name
            testing_by_fuel[name] = testing_by_fuel.get(name, 0.0) + s.testing_liters

    credit_given = CreditGiven.query.filter_by(entry_date=selected_date).all()
    total_credit_given = sum((c.amount for c in credit_given), 0.0)

    bank_sales = BankSale.query.filter_by(entry_date=selected_date).all()
    total_bank_sales = sum((b.amount for b in bank_sales), 0.0)
    # cash_sales is deliberately NOT computed here: total_sales,
    # total_credit_given and total_bank_sales all still gain their
    # pass-through tanker terms further down (see the tanker block), and
    # cash is the remainder once all three are final.

    payments = Receipt.query.filter_by(entry_date=selected_date).all()
    total_payments = sum((p.amount for p in payments), 0.0)

    expenses = Expense.query.filter_by(entry_date=selected_date).order_by(Expense.recorded_at).all()
    total_expenses = sum((e.amount for e in expenses), 0.0)

    purchases = (
        StockPurchase.query.filter_by(entry_date=selected_date).order_by(StockPurchase.recorded_at).all()
    )
    total_purchased_liters = sum((p.liters for p in purchases), 0.0)
    cash_purchases_total = sum((p.cost or 0 for p in purchases if p.payment_type == "cash"), 0.0)

    supplier_payments = SupplierPayment.query.filter_by(entry_date=selected_date).all()
    total_supplier_payments = sum((p.amount for p in supplier_payments), 0.0)

    salaries = SalaryPayment.query.filter_by(entry_date=selected_date).all()
    total_salaries_net = sum((s.net_paid for s in salaries), 0.0)

    sales_returns = SalesReturn.query.filter_by(entry_date=selected_date).all()
    total_sales_returns_liters = sum((sr.liters for sr in sales_returns), 0.0)
    total_sales_returns_amount = sum((sr.amount for sr in sales_returns), 0.0)
    # Only a cash-method return actually draws down the register the same
    # day - a bank-method one hits that bank instead, and a credit-method
    # one just reduces what the customer owes, matching how only cash-paid
    # purchases (not credit ones) subtract from net_cash_flow below.
    cash_sales_returns_total = sum((sr.amount for sr in sales_returns if sr.method == "cash"), 0.0)

    # Non-fuel (lubricant/filter/shop) sales and purchases - counted in
    # UNITS, not liters, since a product isn't measured through a nozzle
    # (see Product.unit's docstring in models.py). Only a cash-method sale
    # or a cash-paid-cash-method purchase actually moves cash-in-hand the
    # same day, exactly the same distinction cash_sales_returns_total and
    # cash_purchases_total above already make for fuel.
    product_sales = ProductSale.query.filter_by(entry_date=selected_date).order_by(ProductSale.recorded_at).all()
    total_product_sales_units = sum((ps.quantity for ps in product_sales), 0.0)
    total_product_sales_amount = sum((ps.amount for ps in product_sales), 0.0)
    cash_product_sales_total = sum((ps.amount for ps in product_sales if ps.method == "cash"), 0.0)

    product_purchases = (
        ProductPurchase.query.filter_by(entry_date=selected_date).order_by(ProductPurchase.recorded_at).all()
    )
    total_product_purchases_units = sum((pp.quantity for pp in product_purchases), 0.0)
    total_product_purchases_cost = sum((pp.total_cost for pp in product_purchases), 0.0)
    cash_product_purchases_total = sum(
        (pp.total_cost for pp in product_purchases if pp.payment_type == "cash" and pp.method == "cash"), 0.0
    )

    # Income that isn't a product sale (rent, a side-business profit
    # share, ...) - cash/bank only, no credit option (see OtherIncome in
    # models.py), so unlike sales returns/product sales above there's no
    # "on account" method to exclude from the cash total.
    other_income_entries = (
        OtherIncome.query.filter_by(entry_date=selected_date).order_by(OtherIncome.recorded_at).all()
    )
    total_other_income = sum((oi.amount for oi in other_income_entries), 0.0)
    cash_other_income_total = sum((oi.amount for oi in other_income_entries if oi.method == "cash"), 0.0)

    # Pass-through tanker deals (see TankerDeal in models.py). Their SALE
    # side is money collected on this date, so it does count in the
    # collected-money cards - Total Sales / Credit Given / Bank Sales,
    # and therefore Cash Sales as the remainder - matching
    # sales_breakdown_for_date() on the Ledger. They are still NEVER
    # folded into total_liters, by_fuel or the tank rows: no fuel left a
    # tank and none was dispensed, so adding them there would inflate
    # litres sold and corrupt every per-litre figure derived from it. The
    # tanker_* keys below stay as they are - they are the PROFIT view
    # (the "Tanker Deal Margin" card), which is a different question from
    # how the money came in.
    tanker_deals = TankerDeal.query.filter_by(entry_date=selected_date).order_by(TankerDeal.recorded_at).all()
    tanker_liters = sum((d.liters for d in tanker_deals), 0.0)
    tanker_revenue = sum((d.sale_amount for d in tanker_deals), 0.0)
    tanker_cost = sum((d.purchase_cost for d in tanker_deals), 0.0)
    tanker_margin = round(tanker_revenue - tanker_cost, 2)
    tanker_credit_sales_total = sum(
        (d.sale_amount for d in tanker_deals if d.sale_payment_type == "credit"), 0.0
    )
    tanker_bank_sales_total = sum(
        (d.sale_amount for d in tanker_deals if d.sale_payment_type == "bank"), 0.0
    )
    cash_tanker_sales_total = sum(
        (d.sale_amount for d in tanker_deals if d.sale_payment_type == "cash"), 0.0
    )
    cash_tanker_purchases_total = sum(
        (d.purchase_cost for d in tanker_deals if d.purchase_payment_type == "cash"), 0.0
    )

    # The pump-only figures are kept under their own names because
    # net_cash_flow below still needs them: it adds the cash-settled
    # tanker sale as its own explicit term, so it must not also read a
    # total_sales that already contains it.
    pump_sales_total = total_sales
    pump_credit_given = total_credit_given
    total_sales += tanker_revenue
    total_credit_given += tanker_credit_sales_total
    total_bank_sales += tanker_bank_sales_total
    # Cash is the remainder, so a cash-settled tanker sale lands in it
    # automatically - there is no fourth tanker term here, and nothing
    # downstream may add cash_tanker_sales_total to cash_sales.
    cash_sales = total_sales - total_credit_given - total_bank_sales

    tanks = Tank.query.order_by(Tank.number).all()
    tank_rows = []
    for t in tanks:
        stock = book_stock(t, selected_date)
        dip = TankDip.query.filter_by(tank_id=t.id, entry_date=selected_date).first()
        tank_rows.append(
            {
                "tank": t,
                "book_stock": stock,
                "dip": dip.dip_liters if dip else None,
                "variance": round(dip.dip_liters - stock, 2) if dip else None,
                "water_cm": dip.water_cm if dip else None,
            }
        )

    # Built from the PUMP-only sales/credit figures plus the cash-settled
    # tanker sale as its own term. Using the tanker-inclusive total_sales /
    # total_credit_given here instead would double-count a cash tanker sale
    # (it is inside total_sales already) and would silently pull a
    # BANK-settled tanker sale into a cash-flow figure. Same reason
    # cash_movement_for_date() dropped its separate "Tanker sales" inflow
    # once the breakdown started carrying it.
    net_cash_flow = (
        pump_sales_total
        - pump_credit_given
        + total_payments
        - total_expenses
        - cash_purchases_total
        - total_supplier_payments
        - total_salaries_net
        - cash_sales_returns_total
        + cash_product_sales_total
        - cash_product_purchases_total
        + cash_other_income_total
        + cash_tanker_sales_total
        - cash_tanker_purchases_total
    )
    outstanding_credit = sum((b for a in Account.query.all() if (b := a.balance) > 0), 0.0)
    # Date-aware closing balances for the SELECTED date, not the all-time
    # figure cash_account_balance()/BankAccount.balance return - the
    # Ledger/Daily Report/Dashboard pages are date-driven, so paging back
    # to an older date must show cash-in-hand and each bank's balance as
    # they stood at the END of that date (see cash_account_balance_as_of()
    # and bank_account_balance_as_of() in ledger_logic.py). Pages with no
    # date to page against (Accounts, bank account detail, Settings) keep
    # calling the plain all-time functions, unchanged.
    cash_balance = cash_account_balance_as_of(get_cash_account(), selected_date)
    bank_accounts = BankAccount.query.order_by(BankAccount.name).all()
    bank_balances_by_id = {b.id: bank_account_balance_as_of(b, selected_date) for b in bank_accounts}

    return {
        "total_sales": total_sales,
        "total_discounts": total_discounts,
        "total_liters": total_liters,
        "total_testing_liters": total_testing_liters,
        "by_fuel": by_fuel,
        "testing_by_fuel": testing_by_fuel,
        "cash_sales": cash_sales,
        "total_credit_given": total_credit_given,
        "bank_sales": bank_sales,
        "total_bank_sales": total_bank_sales,
        "total_payments": total_payments,
        "expenses": expenses,
        "total_expenses": total_expenses,
        "purchases": purchases,
        "total_purchased_liters": total_purchased_liters,
        "total_supplier_payments": total_supplier_payments,
        "tank_rows": tank_rows,
        "net_cash_flow": net_cash_flow,
        "outstanding_credit": outstanding_credit,
        "cash_balance": cash_balance,
        "bank_accounts": bank_accounts,
        "bank_balances_by_id": bank_balances_by_id,
        "salaries": salaries,
        "total_salaries_net": total_salaries_net,
        "sales_returns": sales_returns,
        "total_sales_returns_liters": total_sales_returns_liters,
        "total_sales_returns_amount": total_sales_returns_amount,
        "handover_rows": handover_rows_for_date(selected_date),
        "product_sales": product_sales,
        "total_product_sales_units": total_product_sales_units,
        "total_product_sales_amount": total_product_sales_amount,
        "product_purchases": product_purchases,
        "total_product_purchases_units": total_product_purchases_units,
        "total_product_purchases_cost": total_product_purchases_cost,
        "other_income_entries": other_income_entries,
        "total_other_income": total_other_income,
        "tanker_deals": tanker_deals,
        "tanker_liters": tanker_liters,
        "tanker_revenue": tanker_revenue,
        "tanker_cost": tanker_cost,
        "tanker_margin": tanker_margin,
    }


@app.route("/reports")
@login_required
@owner_required
def reports():
    selected_date = parse_date_param(request.args.get("date"))
    ctx = _reports_context(selected_date)
    return render_template("reports.html", selected_date=selected_date, today=date.today(), **ctx)


@app.route("/reports/export")
@login_required
@owner_required
def reports_export():
    """Mirrors reports() - same figures, same date - as a PDF or Excel
    download instead of a page."""
    fmt = _resolve_export_format()
    selected_date = parse_date_param(request.args.get("date"))
    ctx = _reports_context(selected_date)

    summary_rows = [
        ("Total Sales (Rs)", ctx["total_sales"]),
        ("Liters Sold", ctx["total_liters"]),
        ("Testing (L)", ctx["total_testing_liters"]),
        ("Cash Sales (Rs)", ctx["cash_sales"]),
        ("Bank Sales (Rs)", ctx["total_bank_sales"]),
        ("Credit Given (Rs)", ctx["total_credit_given"]),
        ("Receipts from Customers (Rs)", ctx["total_payments"]),
        ("Sales Returns (L)", ctx["total_sales_returns_liters"]),
        ("Sales Returns (Rs)", ctx["total_sales_returns_amount"]),
        ("Non-Fuel Sales (units)", ctx["total_product_sales_units"]),
        ("Non-Fuel Sales (Rs)", ctx["total_product_sales_amount"]),
        ("Other Income (Rs)", ctx["total_other_income"]),
        ("Tanker Deal Margin (Rs)", ctx["tanker_margin"]),
        ("Product Purchases (units)", ctx["total_product_purchases_units"]),
        ("Product Purchases (Rs)", ctx["total_product_purchases_cost"]),
        ("Expenses (Rs)", ctx["total_expenses"]),
        ("Payments to Suppliers (Rs)", ctx["total_supplier_payments"]),
        ("Salaries Paid Out, Net (Rs)", ctx["total_salaries_net"]),
        ("Net Cash Flow (Rs)", ctx["net_cash_flow"]),
        ("Outstanding Receivables (Rs)", ctx["outstanding_credit"]),
        ("Cash in Hand (Rs)", ctx["cash_balance"]),
    ]
    for b in ctx["bank_accounts"]:
        summary_rows.append((f"{b.name} (Rs)", ctx["bank_balances_by_id"][b.id]))

    blocks = [
        {"type": "summary", "rows": summary_rows},
        {
            "type": "table",
            "heading": "Sales by Fuel Type",
            "columns": ["Fuel", "Liters Sold", "Testing (L)", "Revenue (Rs)"],
            "rows": [
                [name, d["liters"], ctx["testing_by_fuel"].get(name, 0.0), d["revenue"]]
                for name, d in ctx["by_fuel"].items()
            ],
            "align": ["left", "right", "right", "right"],
        },
        {
            "type": "table",
            "heading": "Stock / Dip per Tank",
            "columns": ["Tank", "Fuel", "Book Stock (L)", "Dip Reading (L)", "Variance (L)", "Water (cm)"],
            "rows": [
                [
                    r["tank"].label,
                    r["tank"].fuel_type.name,
                    r["book_stock"],
                    r["dip"] if r["dip"] is not None else "-",
                    r["variance"] if r["variance"] is not None else "-",
                    r["water_cm"] if r["water_cm"] is not None else "-",
                ]
                for r in ctx["tank_rows"]
            ],
            "align": ["left", "left", "right", "right", "right", "right"],
        },
        {
            "type": "table",
            "heading": "Sales Returns",
            "columns": ["Fuel", "Tank", "Liters", "Refund (Rs)", "Method", "Note"],
            "rows": [
                [
                    sr.fuel_type.name,
                    sr.tank.label,
                    sr.liters,
                    sr.amount,
                    {"cash": "Cash", "bank": f"Via {sr.bank_account.name}" if sr.bank_account else "Bank", "credit": f"On account ({sr.account.name})" if sr.account else "On account"}.get(sr.method, sr.method),
                    sr.note or "-",
                ]
                for sr in ctx["sales_returns"]
            ],
            "align": ["left", "left", "right", "right", "left", "left"],
        },
        {
            "type": "table",
            "heading": "Non-Fuel Sales",
            "columns": ["Product", "Units", "Rate (Rs)", "Amount (Rs)", "Method"],
            "rows": [
                [
                    ps.product.label,
                    ps.quantity,
                    ps.retail_rate,
                    ps.amount,
                    {"cash": "Cash", "bank": f"Via {ps.bank_account.name}" if ps.bank_account else "Bank", "credit": f"On account ({ps.account.name})" if ps.account else "On account"}.get(ps.method, ps.method),
                ]
                for ps in ctx["product_sales"]
            ],
            "align": ["left", "right", "right", "right", "left"],
        },
        {
            "type": "table",
            "heading": "Other Income",
            "columns": ["Description", "Method", "Amount (Rs)"],
            "rows": [
                [oi.description, "Via " + oi.bank_account.name if oi.method == "bank" else "Cash", oi.amount]
                for oi in ctx["other_income_entries"]
            ],
            "align": ["left", "left", "right"],
        },
        {
            "type": "table",
            "heading": "Product Purchases",
            "columns": ["Product", "Units", "Unit Cost (Rs)", "Total (Rs)", "Payment"],
            "rows": [
                [pp.product.label, pp.quantity, pp.unit_cost, pp.total_cost, "Credit" if pp.payment_type == "credit" else "Cash"]
                for pp in ctx["product_purchases"]
            ],
            "align": ["left", "right", "right", "right", "left"],
        },
        {
            "type": "table",
            "heading": "Cash Handover - Shift Reconciliation",
            "columns": ["Shift", "Expected Cash (Rs)", "Counted (Rs)", "Variance (Rs)", "Attendant"],
            "rows": [
                [
                    r["shift"].name,
                    r["expected"],
                    r["declared"] if r["declared"] is not None else "-",
                    r["variance"] if r["variance"] is not None else "Not reconciled",
                    r["handover"].attendant.name if r["handover"] and r["handover"].attendant else "-",
                ]
                for r in ctx["handover_rows"]
            ],
            "align": ["left", "right", "right", "right", "left"],
        },
        {
            "type": "table",
            "heading": "Inventory Received",
            "columns": ["Tank", "Liters", "Payment"],
            "rows": [
                [p.tank.label, p.liters, "Credit" if p.payment_type == "credit" else "Cash"]
                for p in ctx["purchases"]
            ],
            "align": ["left", "right", "left"],
        },
        {
            "type": "table",
            "heading": "Expenses",
            "columns": ["Category", "Description", "Paid via", "Amount (Rs)"],
            "rows": [
                [
                    e.category,
                    e.description or "-",
                    f"Via {e.bank_account.name}" if e.method == "bank" else "Cash",
                    e.amount,
                ]
                for e in ctx["expenses"]
            ],
            "align": ["left", "left", "left", "right"],
        },
        {
            "type": "table",
            "heading": "Bank Sales",
            "columns": ["Bank Account", "Amount (Rs)"],
            "rows": [[b.bank_account.name, b.amount] for b in ctx["bank_sales"]],
            "align": ["left", "right"],
        },
        {
            "type": "table",
            "heading": "Salaries",
            "columns": ["Employee", "Period", "Salary (Rs)", "Deducted (Rs)", "Net Paid (Rs)", "Paid via"],
            "rows": [
                [
                    s.account.name,
                    s.period_label or "-",
                    s.gross_amount,
                    s.deduction_amount,
                    s.net_paid,
                    f"Via {s.bank_account.name}" if s.method == "bank" else "Cash",
                ]
                for s in ctx["salaries"]
            ],
            "align": ["left", "left", "right", "right", "right", "left"],
        },
    ]

    return _send_export(
        fmt,
        pdf_title="Daily Report",
        pdf_subtitle=selected_date.strftime("%A, %d %B %Y"),
        xlsx_sheet_name="Daily Report",
        blocks=blocks,
        filename_base=f"petrol-khata-daily-{selected_date.isoformat()}",
    )


def _month_range_from_param(raw_month):
    """Parse a "YYYY-MM" query param into (start, end) dates spanning that
    calendar month, falling back to the current month for anything
    missing or malformed - shared by the Monthly Report page and its
    export so an odd/blank ?month= behaves identically for both."""
    today = date.today()
    try:
        year, month = (int(p) for p in raw_month.split("-"))
        start = date(year, month, 1)
    except (ValueError, AttributeError):
        start = today.replace(day=1)
    end = (start + timedelta(days=31)).replace(day=1) - timedelta(days=1)
    return start, end


def _reports_monthly_context(start, end):
    """Every figure the Monthly Report shows, for one date range - shared
    by the HTML page and its PDF/Excel export.

    Presented as a traditional income statement: Fuel Revenue (gross) -
    Sales Returns = Net Fuel Revenue - Cost of Fuel Sold = Fuel Gross
    Margin; then Product Revenue - Cost of Products Sold = Product Gross
    Margin; Fuel Gross Margin + Product Gross Margin = Total Gross Margin
    + Tanker Deal Margin + Other Income - Expenses - Salaries = Net
    Profit. cogs_for_period() nets sales
    returns into COGS/margin already (see its docstring) - net_revenue
    below is the ONLY other place a return is subtracted, so net_profit
    must never subtract sales_returns_amount again on top of gross_margin,
    or the same refund gets double-counted (that was a real bug here - see
    Phase 1's follow-up fix). "gross_margin" stays FUEL-ONLY (unchanged by
    Phase 2B) precisely so that invariant, and the Trends reconciliation
    that depends on it, can't quietly break just because products now
    exist - product profit is added in separately via product_commission
    on the way to total_gross_margin/net_profit, never folded into
    gross_margin itself."""
    # Each of these is wrapped in float() because coalesce(sum(x), 0) comes
    # back as a Python int when nothing matches (SQLite has no rows to sum,
    # so it falls back to the literal 0) - the HTML template's "%.2f"
    # format hides that, but the PDF export's formatter tells a money
    # figure from a count by its Python type, so an unwrapped int here
    # would render as a bare "0" instead of "0.00".
    # revenue/liters_sold include DirectSale (see models.py) as well as
    # Sale - without this, gross_margin/net_profit below would silently
    # undercount for any period with direct-entry days, even though
    # cogs_for_period() (their COGS counterpart) already folds DirectSale
    # into its own per-fuel gross figures. testing has no DirectSale
    # equivalent at all (see Sale/DirectSale docstrings in models.py).
    revenue = float(
        db.session.query(func.coalesce(func.sum(Sale.total_amount), 0))
        .filter(Sale.entry_date >= start, Sale.entry_date <= end)
        .scalar()
    )
    revenue += float(
        db.session.query(func.coalesce(func.sum(DirectSale.total_amount), 0))
        .filter(DirectSale.entry_date >= start, DirectSale.entry_date <= end)
        .scalar()
    )
    # Sale/DirectSale always record fuel at full list price with no
    # discount capability of their own - net out any discretionary discount
    # given via a CreditGiven row in this period (see
    # credit_discounts_for_period()'s docstring), or revenue (and every
    # figure below derived from it - net_revenue, gross_margin, net_profit)
    # would overstate what was actually earned on a discounted credit sale.
    total_discounts = credit_discounts_for_period(start, end)
    revenue -= total_discounts
    liters_sold = float(
        db.session.query(func.coalesce(func.sum(Sale.liters), 0))
        .filter(Sale.entry_date >= start, Sale.entry_date <= end)
        .scalar()
    )
    liters_sold += float(
        db.session.query(func.coalesce(func.sum(DirectSale.liters), 0))
        .filter(DirectSale.entry_date >= start, DirectSale.entry_date <= end)
        .scalar()
    )
    testing_liters = float(
        db.session.query(func.coalesce(func.sum(Sale.testing_liters), 0))
        .filter(Sale.entry_date >= start, Sale.entry_date <= end)
        .scalar()
    )
    cogs, cogs_detail = cogs_for_period(start, end)
    expenses_total = float(
        db.session.query(func.coalesce(func.sum(Expense.amount), 0))
        .filter(Expense.entry_date >= start, Expense.entry_date <= end)
        .scalar()
    )
    salaries_total = float(
        db.session.query(func.coalesce(func.sum(SalaryPayment.gross_amount), 0))
        .filter(SalaryPayment.entry_date >= start, SalaryPayment.entry_date <= end)
        .scalar()
    )
    expenses_by_category = (
        db.session.query(Expense.category, func.sum(Expense.amount))
        .filter(Expense.entry_date >= start, Expense.entry_date <= end)
        .group_by(Expense.category)
        .order_by(func.sum(Expense.amount).desc())
        .all()
    )

    credit_given = float(
        db.session.query(func.coalesce(func.sum(CreditGiven.amount), 0))
        .filter(CreditGiven.entry_date >= start, CreditGiven.entry_date <= end)
        .scalar()
    )
    receipts_total = float(
        db.session.query(func.coalesce(func.sum(Receipt.amount), 0))
        .filter(Receipt.entry_date >= start, Receipt.entry_date <= end)
        .scalar()
    )
    purchases_liters = float(
        db.session.query(func.coalesce(func.sum(StockPurchase.liters), 0))
        .filter(StockPurchase.entry_date >= start, StockPurchase.entry_date <= end)
        .scalar()
    )
    purchases_cost = float(
        db.session.query(func.coalesce(func.sum(StockPurchase.cost), 0))
        .filter(StockPurchase.entry_date >= start, StockPurchase.entry_date <= end)
        .scalar()
    )
    # Per-fuel breakdown of those same two all-fuel totals - purchases_liters
    # and purchases_cost above must equal the sum of this list's liters/cost.
    purchases_by_fuel = stock_purchases_by_fuel_for_period(start, end)
    sales_returns_liters = float(
        db.session.query(func.coalesce(func.sum(SalesReturn.liters), 0))
        .filter(SalesReturn.entry_date >= start, SalesReturn.entry_date <= end)
        .scalar()
    )
    sales_returns_amount = float(
        db.session.query(func.coalesce(func.sum(SalesReturn.amount), 0))
        .filter(SalesReturn.entry_date >= start, SalesReturn.entry_date <= end)
        .scalar()
    )

    # Revenue stays available as the gross figure (what was actually rung
    # up on the nozzles) for its own line on the statement; net_revenue -
    # gross minus the same sales returns cogs_for_period() already netted
    # into COGS/margin - is what Gross Margin is actually built from, so
    # a return is subtracted exactly once on the way to Net Profit.
    net_revenue = round(revenue - sales_returns_amount, 2)
    gross_margin = round(net_revenue - cogs, 2)

    # Products have no weighted-average cost lookup at all (see
    # product_margin_for_period()'s docstring) - every line already
    # carries its own exact cost, so there's nothing here that mirrors
    # cogs_for_period()'s "as of end date" argument.
    product_revenue, product_cost, product_commission, product_category_detail = product_margin_for_period(
        start, end
    )
    total_gross_margin = round(gross_margin + product_commission, 2)
    # Income that isn't a product sale (rent, a side-business profit
    # share, ...) - has no associated cost, so unlike product_commission
    # above it's a pure addition, not a revenue-minus-cost margin.
    other_income_total = float(
        db.session.query(func.coalesce(func.sum(OtherIncome.amount), 0))
        .filter(OtherIncome.entry_date >= start, OtherIncome.entry_date <= end)
        .scalar()
    )
    # No further "- sales_returns_amount" here: net_revenue (and therefore
    # gross_margin) is already net of returns. Subtracting it again would
    # double-count the same refund - that was the bug this comment is
    # guarding against. Net Profit is Total Gross Margin (fuel + product)
    # plus Other Income, minus operating costs.
    # Pass-through tanker deals (see TankerDeal in models.py) - their own
    # revenue/cost/margin, deliberately kept OUT of revenue, liters_sold,
    # cogs and gross_margin above. Nothing was dispensed from a tank and
    # the cost is this one tanker's exact invoice, not a weighted average,
    # so folding it into those would both inflate litres sold and make
    # Cost of Fuel Sold mean two different things at once. It is a real
    # profit though, so it is added into net_profit as its own line, the
    # same pure-addition way other_income_total is.
    tanker_liters = float(
        db.session.query(func.coalesce(func.sum(TankerDeal.liters), 0))
        .filter(TankerDeal.entry_date >= start, TankerDeal.entry_date <= end)
        .scalar()
    )
    tanker_revenue = float(
        db.session.query(func.coalesce(func.sum(TankerDeal.sale_amount), 0))
        .filter(TankerDeal.entry_date >= start, TankerDeal.entry_date <= end)
        .scalar()
    )
    tanker_cost = float(
        db.session.query(func.coalesce(func.sum(TankerDeal.purchase_cost), 0))
        .filter(TankerDeal.entry_date >= start, TankerDeal.entry_date <= end)
        .scalar()
    )
    tanker_margin = round(tanker_revenue - tanker_cost, 2)
    net_profit = round(
        total_gross_margin + tanker_margin + other_income_total - expenses_total - salaries_total, 2
    )

    return {
        "revenue": revenue,
        "total_discounts": total_discounts,
        "net_revenue": net_revenue,
        "liters_sold": liters_sold,
        "testing_liters": testing_liters,
        "cogs": cogs,
        "cogs_detail": cogs_detail,
        "gross_margin": gross_margin,
        "product_revenue": product_revenue,
        "product_cost": product_cost,
        "product_commission": product_commission,
        "product_category_detail": product_category_detail,
        "total_gross_margin": total_gross_margin,
        "tanker_liters": tanker_liters,
        "tanker_revenue": tanker_revenue,
        "tanker_cost": tanker_cost,
        "tanker_margin": tanker_margin,
        "other_income_total": other_income_total,
        "expenses_total": expenses_total,
        "expenses_by_category": expenses_by_category,
        "salaries_total": salaries_total,
        "net_profit": net_profit,
        "credit_given": credit_given,
        "receipts_total": receipts_total,
        "purchases_liters": purchases_liters,
        "purchases_cost": purchases_cost,
        "purchases_by_fuel": purchases_by_fuel,
        "sales_returns_liters": sales_returns_liters,
        "sales_returns_amount": sales_returns_amount,
        "attendant_variances": attendant_variance_summary(start, end),
    }


def _fuel_color_map(cogs_detail, purchases_by_fuel):
    """The SAME fuel must be the SAME colour in both Monthly Report donuts
    and both card grids, so colour is keyed on the fuel NAME in a stable
    alphabetical order, not on per-donut rank (where the 2nd-biggest fuel
    in one donut could otherwise land on a different colour than the
    2nd-biggest in the other). Palette and ordering match
    revenue_mix_for_date()'s (chart-2, chart-4, chart-3, chart-6,
    chart-1), with chart-5 appended for a 6th fuel type."""
    palette = [
        "var(--chart-2)", "var(--chart-4)", "var(--chart-3)",
        "var(--chart-6)", "var(--chart-1)", "var(--chart-5)",
    ]
    names = sorted({r["fuel"] for r in cogs_detail} | {r["fuel"] for r in purchases_by_fuel})
    return {name: palette[i % len(palette)] for i, name in enumerate(names)}


def _reports_monthly_extras(start, end, ctx):
    """Everything the redesigned Monthly Report page needs that is
    deliberately NOT in _reports_monthly_context() - because
    month_to_date_pace() calls that context function three times on every
    Dashboard load, and account_positions() / _cash_daily_net_changes() /
    a prior-month context call are each far too expensive to run 3x for a
    card that only reads net_revenue/net_profit. This function is called
    exactly twice: once by reports_monthly(), once (partially) by
    reports_monthly_export()."""
    today = date.today()

    # 1. Period cash summary (month-scoped, honest).
    cash_period = cash_movement_for_period(start, end)

    # 2. Payables / receivables. ONE account walk, shared by both.
    positions = account_positions(today, include_aging=False)
    wc = working_capital(today, positions=positions)
    payables_rows = payables_schedule(positions, today)
    receivable_rows = sorted(
        (
            {"account": p["account"], "balance": round(p["balance"], 2)}
            for p in positions
            if p["balance"] > 0
        ),
        key=lambda r: r["balance"],
        reverse=True,
    )

    # 3. Prior FULL month context, for the profit-vs-last-month narrative rule.
    prior_end = start - timedelta(days=1)
    prior_start = prior_end.replace(day=1)
    prior_ctx = _reports_monthly_context(prior_start, prior_end)
    prior_month_label = prior_start.strftime("%B %Y")

    # 4. Best sales day inside the month.
    best_day = best_sales_day_for_period(start, end)

    # 5. Stable per-fuel colour map, shared by both donuts and both card grids.
    fuel_colors = _fuel_color_map(ctx["cogs_detail"], ctx["purchases_by_fuel"])

    # 6. Donut segments (money-valued only - donut_chart() prints "Rs").
    fuel_sold_segments = sorted(
        (
            {"label": r["fuel"], "amount": round(r["revenue"], 2), "color": fuel_colors[r["fuel"]]}
            for r in ctx["cogs_detail"]
            if r["revenue"] > 0
        ),
        key=lambda s: s["amount"],
        reverse=True,
    )
    stock_ordered_segments = sorted(
        (
            {"label": r["fuel"], "amount": round(r["cost"], 2), "color": fuel_colors[r["fuel"]]}
            for r in ctx["purchases_by_fuel"]
            if r["cost"] > 0
        ),
        key=lambda s: s["amount"],
        reverse=True,
    )

    # 7. Narrative.
    narrative = monthly_narrative(start, end, ctx, prior_ctx, prior_month_label, best_day)

    # 8. Step-by-step profit walk.
    profit_steps = profit_walkthrough(ctx)

    return {
        "cash_period": cash_period,
        "wc": wc,
        "payables_rows": payables_rows,
        "payables_total": wc["payables"],
        "receivable_rows": receivable_rows,
        "receivables_total": wc["receivables"],
        "balances_as_of": today,
        "prior_ctx": prior_ctx,
        "prior_month_label": prior_month_label,
        "best_day": best_day,
        "fuel_colors": fuel_colors,
        "fuel_sold_donut": charts.donut_chart(fuel_sold_segments),
        "stock_ordered_donut": charts.donut_chart(stock_ordered_segments),
        "narrative": narrative,
        "profit_steps": profit_steps,
        "thin_margin_per_liter": THIN_MARGIN_PER_LITER,
    }


def month_to_date_pace(as_of_date):
    """MTD revenue/profit pace for the Dashboard's "This month so far"
    card: what's happened this month, what the SAME number of days into
    last month looked like (a fair comparison - 18 partial days of August
    against all 31 days of July would always look like August is losing,
    even at an identical daily run rate), last month's full total (to
    answer "will we beat last month?"), and a straight-line projection to
    month end.

    Built entirely from _reports_monthly_context() - called up to three
    times (this month to date, last month to the same day-count, last
    month in full) rather than reimplementing any margin/COGS/sales-returns
    arithmetic here. That function's own docstring documents the
    sales-returns double-count bug this phase must not reopen; three calls
    to it (a handful of grouped, bounded queries each) is a completely
    different order of cost from the O(accounts) problem Part 0 exists to
    fix, so this stays well inside the "bounded queries" constraint.

    revenue/profit figures used throughout are the NET ones
    (_reports_monthly_context()'s "net_revenue"/"net_profit" - already net
    of sales returns) rather than the gross "revenue" key, so the pace
    comparison and the projection are never contaminated by a return that
    hasn't been netted out yet.

    days_elapsed is 1..days_in_month, inclusive of as_of_date itself (day 1
    of the month has days_elapsed == 1, not 0), so the projection formula
    (mtd / days_elapsed * days_in_month) is never a divide-by-zero."""
    month_start = as_of_date.replace(day=1)
    days_elapsed = (as_of_date - month_start).days + 1
    month_end = (month_start + timedelta(days=31)).replace(day=1) - timedelta(days=1)
    days_in_month = month_end.day

    prior_month_end = month_start - timedelta(days=1)
    prior_month_start = prior_month_end.replace(day=1)
    prior_days_in_month = prior_month_end.day
    # Clamped so e.g. "day 31" of a 31-day month doesn't overrun a 28/29/30
    # day February when walking back one month - the comparison then uses
    # as many days as the prior month actually has, which is the fairest
    # number available rather than an out-of-range date.
    prior_same_day_count = min(days_elapsed, prior_days_in_month)
    prior_mtd_end = prior_month_start + timedelta(days=prior_same_day_count - 1)

    mtd_ctx = _reports_monthly_context(month_start, as_of_date)
    prior_mtd_ctx = _reports_monthly_context(prior_month_start, prior_mtd_end)
    prior_full_ctx = _reports_monthly_context(prior_month_start, prior_month_end)

    mtd_revenue = mtd_ctx["net_revenue"]
    mtd_profit = mtd_ctx["net_profit"]
    prior_mtd_revenue = prior_mtd_ctx["net_revenue"]
    prior_mtd_profit = prior_mtd_ctx["net_profit"]
    prior_full_revenue = prior_full_ctx["net_revenue"]
    prior_full_profit = prior_full_ctx["net_profit"]

    projected_revenue = round(mtd_revenue / days_elapsed * days_in_month, 2)
    projected_profit = round(mtd_profit / days_elapsed * days_in_month, 2)

    def _delta_pct(current, prior):
        # abs(prior) as the denominator keeps the sign of the delta
        # meaningful even when prior itself was a loss (a negative
        # prior_mtd_profit that improves toward zero should read as a
        # POSITIVE delta, not a confusing negative-over-negative).
        if not prior:
            return None
        return round((current - prior) / abs(prior) * 100, 1)

    revenue_delta_pct = _delta_pct(mtd_revenue, prior_mtd_revenue)
    profit_delta_pct = _delta_pct(mtd_profit, prior_mtd_profit)

    return {
        "month_start": month_start,
        "days_elapsed": days_elapsed,
        "days_in_month": days_in_month,
        "mtd_revenue": mtd_revenue,
        "mtd_profit": mtd_profit,
        "prior_mtd_revenue": prior_mtd_revenue,
        "prior_mtd_profit": prior_mtd_profit,
        "prior_full_revenue": prior_full_revenue,
        "prior_full_profit": prior_full_profit,
        "projected_revenue": projected_revenue,
        "projected_profit": projected_profit,
        "revenue_delta_pct": revenue_delta_pct,
        "profit_delta_pct": profit_delta_pct,
        "ahead": profit_delta_pct is not None and profit_delta_pct >= 0,
    }


@app.route("/reports/monthly")
@login_required
@owner_required
def reports_monthly():
    """Month-end closing statement. Unlike the Daily Report (which is a
    cash-movement view of one day), this is a period profit view: revenue
    against the cost of the fuel actually sold (COGS via weighted average
    purchase cost), then operating costs, then net."""
    today = date.today()
    start, end = _month_range_from_param(request.args.get("month", ""))
    ctx = _reports_monthly_context(start, end)
    extras = _reports_monthly_extras(start, end, ctx)

    # Previous/next month links, capped so you can't page into the future.
    prev_month = (start - timedelta(days=1)).replace(day=1)
    next_month = end + timedelta(days=1)
    return render_template(
        "reports_monthly.html",
        start=start,
        end=end,
        month_label=start.strftime("%B %Y"),
        prev_month=prev_month.strftime("%Y-%m"),
        next_month=next_month.strftime("%Y-%m") if next_month <= today else None,
        today=today,
        **ctx,
        **extras,
    )


@app.route("/reports/monthly/export")
@login_required
@owner_required
def reports_monthly_export():
    """Mirrors reports_monthly() - same figures, same month - as a PDF or
    Excel download instead of a page."""
    fmt = _resolve_export_format()
    start, end = _month_range_from_param(request.args.get("month", ""))
    ctx = _reports_monthly_context(start, end)
    cash_period = cash_movement_for_period(start, end)

    blocks = [
        {
            # Traditional income-statement order - each "less:" line
            # subtracts from the one above it, ending at Net Profit.
            "type": "summary",
            "rows": [
                ("Fuel Revenue, Gross (Rs)", ctx["revenue"]),
                ("Liters Sold, Gross", ctx["liters_sold"]),
                ("Testing (L)", ctx["testing_liters"]),
                ("Less: Sales Returns (Rs)", ctx["sales_returns_amount"]),
                ("Sales Returns (L)", ctx["sales_returns_liters"]),
                ("Net Fuel Revenue (Rs)", ctx["net_revenue"]),
                ("Less: Cost of Fuel Sold (Rs)", ctx["cogs"]),
                ("Fuel Gross Margin (Rs)", ctx["gross_margin"]),
                ("Product Revenue (Rs)", ctx["product_revenue"]),
                ("Less: Cost of Products Sold (Rs)", ctx["product_cost"]),
                ("Product Gross Margin (Rs)", ctx["product_commission"]),
                ("Total Gross Margin (Rs)", ctx["total_gross_margin"]),
                ("Tanker Deal Margin (Rs)", ctx["tanker_margin"]),
                ("Other Income (Rs)", ctx["other_income_total"]),
                ("Less: Expenses (Rs)", ctx["expenses_total"]),
                ("Less: Salaries (Rs)", ctx["salaries_total"]),
                ("Net Profit (Rs)", ctx["net_profit"]),
            ],
        },
        {
            "type": "table",
            "heading": "Margin by Fuel Type",
            "columns": [
                "Fuel", "Gross Revenue (Rs)", "Returns (L)", "Returns (Rs)",
                "Net Liters Sold", "Net Revenue (Rs)", "Avg Cost / L", "Cost (Rs)", "Margin (Rs)",
            ],
            "rows": [
                [
                    r["fuel"], r["gross_revenue"], r["returns_liters"], r["returns_amount"],
                    r["liters"], r["revenue"], r["unit_cost"], r["cost"], r["margin"],
                ]
                for r in ctx["cogs_detail"]
            ],
            "align": ["left", "right", "right", "right", "right", "right", "right", "right", "right"],
        },
        {
            "type": "table",
            "heading": "Margin by Product Category",
            "columns": ["Category", "Units", "Revenue (Rs)", "Cost (Rs)", "Margin (Rs)", "Margin %"],
            "rows": [
                [
                    r["category"].capitalize(), r["quantity"], r["revenue"], r["cost"], r["margin"],
                    round(r["margin"] / r["revenue"] * 100, 1) if r["revenue"] else 0.0,
                ]
                for r in ctx["product_category_detail"]
            ],
            "align": ["left", "right", "right", "right", "right", "right"],
        },
        {
            "type": "table",
            "heading": "Stock Ordered by Fuel Type",
            "columns": ["Fuel", "Litres Received", "Total Cost (Rs)", "Avg Cost / L"],
            "rows": [
                [r["fuel"], r["liters"], r["cost"], r["avg_cost_per_liter"]]
                for r in ctx["purchases_by_fuel"]
            ],
            "align": ["left", "right", "right", "right"],
        },
        {
            "type": "table",
            "heading": "Expenses by Category",
            "columns": ["Category", "Amount (Rs)"],
            "rows": [[category, amount] for category, amount in ctx["expenses_by_category"]],
            "align": ["left", "right"],
        },
        {
            "type": "table",
            "heading": "Credit & Stock Movement",
            "columns": ["Metric", "Value"],
            "rows": [
                ["Credit given to customers (Rs)", ctx["credit_given"]],
                ["Payments received (Rs)", ctx["receipts_total"]],
                ["Fuel purchased (L)", ctx["purchases_liters"]],
                ["Spent on fuel purchases (Rs)", ctx["purchases_cost"]],
            ],
            "align": ["left", "right"],
        },
        {
            "type": "table",
            "heading": "Cash Summary",
            "columns": ["Line", "Amount (Rs)"],
            "rows": (
                [["Opening cash in hand", cash_period["opening"]]]
                + [[f"+ {row['label']}", row["amount"]] for row in cash_period["inflows"]]
                + [["Total cash received", cash_period["total_in"]]]
                + [[f"- {row['label']}", row["amount"]] for row in cash_period["outflows"]]
                + [
                    ["Total cash paid", cash_period["total_out"]],
                    ["Closing cash in hand", cash_period["closing"]],
                ]
            ),
            "align": ["left", "right"],
        },
        {
            "type": "table",
            "heading": "Cash Variance by Attendant",
            "columns": ["Attendant", "Shifts Reconciled", "Shifts Short", "Net Variance (Rs)"],
            "rows": [
                [r["name"], r["shifts"], r["shortfalls"], r["total_variance"]]
                for r in ctx["attendant_variances"]
            ],
            "align": ["left", "right", "right", "right"],
        },
    ]

    return _send_export(
        fmt,
        pdf_title=f"Monthly Report - {start.strftime('%B %Y')}",
        pdf_subtitle=f"{start.strftime('%d %b')} to {end.strftime('%d %b %Y')}",
        xlsx_sheet_name="Monthly Report",
        blocks=blocks,
        filename_base=f"petrol-khata-monthly-{start.strftime('%Y-%m')}",
    )


RANGE_OPTIONS = {15: "Past 15 days", 30: "Past month", 90: "Past 3 months", 365: "Past year"}


@app.route("/reports/trends")
@login_required
@owner_required
def reports_trends():
    days = request.args.get("range", 15, type=int)
    if days not in RANGE_OPTIONS:
        days = 15

    end = date.today()
    start = end - timedelta(days=days - 1)
    all_dates = [start + timedelta(days=i) for i in range(days)]
    label_fmt = "%b %d" if days <= 90 else "%b '%y"

    def group_sum(model, value_col, date_col="entry_date"):
        return _group_sum_by_day(model, value_col, start, end, date_col)

    sales_by_day = group_sum(Sale, Sale.total_amount)
    # Fold DirectSale (see models.py) into the same by-day revenue totals
    # Sale contributes to - every series/total built from sales_by_day
    # below (cash_series, profit_series, totals.total_sales, ...) must
    # reflect BOTH entry methods, exactly like sales_breakdown_for_date()
    # and cash_account_balance() already do.
    for d, v in group_sum(DirectSale, DirectSale.total_amount).items():
        sales_by_day[d] = sales_by_day.get(d, 0) + v
    # Net out per-day discretionary discounts on discounted CreditGiven rows
    # (see credit_discounts_for_period()'s docstring) - sales_by_day above is
    # gross Sale/DirectSale (always list price), so without this cash_series,
    # profit_series, and totals.total_sales (all built from sales_by_day)
    # would overstate revenue/cash by the discount. Row-by-row, grouped by
    # date, mirroring dashboard_trend_series()'s own _profit_series_for_window()
    # fix in ledger_logic.py rather than one credit_discounts_for_period()
    # call per day.
    for entry_date_val, liters, price, amount in (
        db.session.query(CreditGiven.entry_date, CreditGiven.liters, CreditGiven.price_per_liter, CreditGiven.amount)
        .filter(CreditGiven.entry_date >= start, CreditGiven.entry_date <= end)
        .all()
    ):
        list_amount = round(liters * price, 2)
        if abs(amount - list_amount) > 0.01:
            sales_by_day[entry_date_val] = sales_by_day.get(entry_date_val, 0) - (list_amount - amount)
    credit_by_day = group_sum(CreditGiven, CreditGiven.amount)
    payments_by_day = group_sum(Receipt, Receipt.amount)
    expenses_by_day = group_sum(Expense, Expense.amount)
    purchase_liters_by_day = group_sum(StockPurchase, StockPurchase.liters)
    salaries_by_day = group_sum(SalaryPayment, SalaryPayment.gross_amount)
    returns_amount_by_day = group_sum(SalesReturn, SalesReturn.amount)
    product_revenue_by_day = group_sum(ProductSale, ProductSale.amount)
    other_income_by_day = group_sum(OtherIncome, OtherIncome.amount)
    # Unlike cogs_by_day below, this needs no weighted-average/unit-cost
    # lookup at all - every ProductSale row already carries its own
    # purchase_rate, snapshotted at the moment of sale (see
    # product_margin_for_period()'s docstring), so quantity x that row's
    # own rate, summed per day, is already exact.
    product_cost_by_day = {
        r[0]: r[1] or 0
        for r in (
            db.session.query(ProductSale.entry_date, func.sum(ProductSale.quantity * ProductSale.purchase_rate))
            .filter(ProductSale.entry_date >= start, ProductSale.entry_date <= end)
            .group_by(ProductSale.entry_date)
            .all()
        )
    }

    # Daily profit values fuel at its weighted-average purchase cost (COGS)
    # rather than subtracting whatever was bought that day - otherwise a
    # tanker delivery shows as a huge "loss" on the day it arrives even
    # though the stock is still in the tank waiting to be sold. It also
    # nets sales returns into both revenue and COGS the same way
    # cogs_for_period()/_reports_monthly_context() do (net revenue minus
    # net COGS minus expenses minus salaries) - the Monthly Report and
    # this page used to disagree on the same month's profit because this
    # series ignored returns entirely; see the Phase 1 follow-up fix and
    # its regression test asserting the two pages reconcile. Product
    # margin (product revenue minus product cost, both exact per line -
    # see product_cost_by_day above) is added in on top, the same total
    # _reports_monthly_context() folds into total_gross_margin/net_profit,
    # so the two keep reconciling now that products exist too.
    unit_costs = {ft.id: weighted_avg_cost(ft, end) for ft in FuelType.query.all()}
    sold_liters_by_day_fuel = {}
    for entry_date_val, fuel_type_id, liters in (
        db.session.query(Sale.entry_date, Tank.fuel_type_id, func.sum(Sale.liters))
        .join(Nozzle, Sale.nozzle_id == Nozzle.id)
        .join(Tank, Nozzle.tank_id == Tank.id)
        .filter(Sale.entry_date >= start, Sale.entry_date <= end)
        .group_by(Sale.entry_date, Tank.fuel_type_id)
        .all()
    ):
        sold_liters_by_day_fuel[(entry_date_val, fuel_type_id)] = liters or 0
    # DirectSale is already tank-keyed - no Nozzle join needed.
    for entry_date_val, fuel_type_id, liters in (
        db.session.query(DirectSale.entry_date, Tank.fuel_type_id, func.sum(DirectSale.liters))
        .join(Tank, DirectSale.tank_id == Tank.id)
        .filter(DirectSale.entry_date >= start, DirectSale.entry_date <= end)
        .group_by(DirectSale.entry_date, Tank.fuel_type_id)
        .all()
    ):
        key = (entry_date_val, fuel_type_id)
        sold_liters_by_day_fuel[key] = sold_liters_by_day_fuel.get(key, 0) + (liters or 0)
    returned_liters_by_day_fuel = {}
    for entry_date_val, fuel_type_id, liters in (
        db.session.query(SalesReturn.entry_date, SalesReturn.fuel_type_id, func.sum(SalesReturn.liters))
        .filter(SalesReturn.entry_date >= start, SalesReturn.entry_date <= end)
        .group_by(SalesReturn.entry_date, SalesReturn.fuel_type_id)
        .all()
    ):
        returned_liters_by_day_fuel[(entry_date_val, fuel_type_id)] = liters or 0

    # Net per (day, fuel) - a return reverses cost on the day it actually
    # happened (its own dated ledger entry), not retroactively on the
    # original sale's day. Summed across the whole window this always
    # equals cogs_for_period()'s per-fuel (gross sold - gross returned) x
    # unit cost, since that arithmetic distributes the same way regardless
    # of which day either side falls on - except in the edge case where
    # returns exceed sales for one fuel across the ENTIRE window, where
    # cogs_for_period() floors that fuel's cost at 0 for the period; a
    # day-by-day series has no single "period" to floor against, so it
    # isn't reproduced here. Not reachable in normal use (a fuel can't be
    # returned more than was ever sold of it).
    cogs_by_day = {}
    for key in set(sold_liters_by_day_fuel) | set(returned_liters_by_day_fuel):
        entry_date_val, fuel_type_id = key
        net_liters = sold_liters_by_day_fuel.get(key, 0) - returned_liters_by_day_fuel.get(key, 0)
        cogs_by_day[entry_date_val] = round(
            cogs_by_day.get(entry_date_val, 0) + net_liters * unit_costs.get(fuel_type_id, 0), 2
        )

    labels = [d.strftime(label_fmt) for d in all_dates]
    cash_series = [round(sales_by_day.get(d, 0) - credit_by_day.get(d, 0), 2) for d in all_dates]
    credit_series = [round(credit_by_day.get(d, 0), 2) for d in all_dates]
    receipts_series = [round(payments_by_day.get(d, 0), 2) for d in all_dates]
    expenses_series = [round(expenses_by_day.get(d, 0), 2) for d in all_dates]
    purchases_series = [round(purchase_liters_by_day.get(d, 0), 2) for d in all_dates]
    product_margin_series = [
        round(product_revenue_by_day.get(d, 0) - product_cost_by_day.get(d, 0), 2) for d in all_dates
    ]
    # Per-day pass-through tanker margin (see TankerDeal in models.py),
    # grouped by date the same way every other term here is, and added to
    # profit as its OWN addend - never into sales_by_day (which also backs
    # cash_series and the by-fuel litre/rupee charts, all of which mean
    # fuel actually dispensed) and never into cogs_by_day (a weighted
    # per-litre cost this deal has no part in). This mirrors
    # _profit_series_for_window() in ledger_logic.py line for line, and is
    # what keeps this page reconciling with _reports_monthly_context()'s
    # net_profit, which adds tanker_margin the same separate way.
    tanker_margin_by_day = {}
    for entry_date_val, sale_amount, purchase_cost in (
        db.session.query(TankerDeal.entry_date, TankerDeal.sale_amount, TankerDeal.purchase_cost)
        .filter(TankerDeal.entry_date >= start, TankerDeal.entry_date <= end)
        .all()
    ):
        tanker_margin_by_day[entry_date_val] = round(
            tanker_margin_by_day.get(entry_date_val, 0) + (sale_amount - purchase_cost), 2
        )
    profit_series = [
        round(
            (sales_by_day.get(d, 0) - returns_amount_by_day.get(d, 0))  # net fuel revenue
            - cogs_by_day.get(d, 0)  # net fuel COGS
            - expenses_by_day.get(d, 0)
            - salaries_by_day.get(d, 0)
            + product_margin_series[i]
            + other_income_by_day.get(d, 0)
            + tanker_margin_by_day.get(d, 0),
            2,
        )
        for i, d in enumerate(all_dates)
    ]

    # Chart colors are theme tokens (var(--chart-N)), not literal hex, so
    # every chart re-themes with the rest of the app. The mapping keeps
    # each old hex's "identity" wherever it appeared (green=cash-positive
    # -> chart-2, red=credit/expense -> chart-4, indigo=neutral secondary
    # series -> chart-3) so a color still means the same thing across every
    # chart on this page, same intent as revenue_mix_for_date()'s own
    # docstring in ledger_logic.py.
    sales_chart = charts.stacked_bar_chart(
        cash_series, credit_series, labels, ["var(--chart-2)", "var(--chart-4)"], ["Cash Sales", "Credit Given"]
    )
    cashflow_chart = charts.line_chart(
        [receipts_series, expenses_series], labels, ["var(--chart-3)", "var(--chart-4)"], ["Receipts", "Expenses"]
    )
    purchases_chart = charts.bar_chart(purchases_series, labels, "var(--chart-3)")
    profit_chart = charts.line_chart([profit_series], labels, ["var(--chart-2)"], ["Profit (Est.)"])

    tanks = Tank.query.order_by(Tank.number).all()
    tank_colors = [
        "var(--chart-3)", "var(--chart-2)", "var(--chart-1)",
        "var(--chart-4)", "var(--chart-6)", "var(--chart-5)",
    ]
    stock_series_list = [stock_series(t, all_dates) for t in tanks]
    stock_chart = (
        charts.line_chart(
            stock_series_list,
            labels,
            [tank_colors[i % len(tank_colors)] for i in range(len(tanks))],
            [t.label for t in tanks],
        )
        if tanks
        else ""
    )

    fuel_types = FuelType.query.order_by(FuelType.name).all()
    fuel_sold_by_day = {}
    for ft in fuel_types:
        rows = (
            db.session.query(Sale.entry_date, func.sum(Sale.liters))
            .join(Nozzle, Sale.nozzle_id == Nozzle.id)
            .join(Tank, Nozzle.tank_id == Tank.id)
            .filter(Tank.fuel_type_id == ft.id, Sale.entry_date >= start, Sale.entry_date <= end)
            .group_by(Sale.entry_date)
            .all()
        )
        by_day = {r[0]: r[1] or 0 for r in rows}
        # DirectSale is already tank-keyed - no Nozzle join needed.
        direct_rows = (
            db.session.query(DirectSale.entry_date, func.sum(DirectSale.liters))
            .join(Tank, DirectSale.tank_id == Tank.id)
            .filter(Tank.fuel_type_id == ft.id, DirectSale.entry_date >= start, DirectSale.entry_date <= end)
            .group_by(DirectSale.entry_date)
            .all()
        )
        for d, liters in direct_rows:
            by_day[d] = by_day.get(d, 0) + (liters or 0)
        fuel_sold_by_day[ft.name] = by_day
    fuel_sold_series_list = [
        [round(fuel_sold_by_day[ft.name].get(d, 0), 2) for d in all_dates] for ft in fuel_types
    ]
    fuel_sold_totals = {ft.name: round(sum(fuel_sold_by_day[ft.name].values()), 2) for ft in fuel_types}
    fuel_sold_chart = (
        charts.line_chart(
            fuel_sold_series_list,
            labels,
            [tank_colors[i % len(tank_colors)] for i in range(len(fuel_types))],
            [ft.name for ft in fuel_types],
        )
        if fuel_types
        else ""
    )

    totals = dict(
        total_sales=sum(sales_by_day.values()),
        total_credit_given=sum(credit_by_day.values()),
        total_receipts=sum(payments_by_day.values()),
        total_expenses=sum(expenses_by_day.values()),
        total_purchased_liters=sum(purchase_liters_by_day.values()),
        total_cogs=round(sum(cogs_by_day.values()), 2),
        total_salaries=round(sum(salaries_by_day.values()), 2),
        total_sales_returns=round(sum(returns_amount_by_day.values()), 2),
        total_product_revenue=round(sum(product_revenue_by_day.values()), 2),
        total_product_cost=round(sum(product_cost_by_day.values()), 2),
        total_product_margin=round(sum(product_margin_series), 2),
        total_other_income=round(sum(other_income_by_day.values()), 2),
        total_profit=round(sum(profit_series), 2),
    )

    return render_template(
        "trends.html",
        days=days,
        range_options=RANGE_OPTIONS,
        sales_chart=sales_chart,
        cashflow_chart=cashflow_chart,
        purchases_chart=purchases_chart,
        stock_chart=stock_chart,
        fuel_sold_chart=fuel_sold_chart,
        fuel_sold_totals=fuel_sold_totals,
        profit_chart=profit_chart,
        totals=totals,
    )
