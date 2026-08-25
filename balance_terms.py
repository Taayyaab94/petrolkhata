"""Single source of truth for the signed term lists that Account.balance,
BankAccount.balance, and bank_account_balance_as_of() all compute over.

Before this module existed, the same signed term list was hand-copied in
two to four places, in two different styles (Python-summed relationship
backrefs vs. explicit SQL sums), and could drift out of sync one term at
a time - see bank_account_balance_as_of()'s and credit_aging()'s
docstrings in ledger_logic.py for two documented cases of that actually
happening. ACCOUNT_TERMS and BANK_TERMS below are transcribed term for
term, sign for sign, filter for filter, from the current
Account.balance / BankAccount.balance / bank_account_balance_as_of()
bodies - nothing here was re-derived from what a sign "should" be.

Deliberately imports nothing from models.py or ledger_logic.py at module
level - both of those modules import this one, so a module-level import
back here would be a cycle. Model classes are referred to by NAME
(strings) and resolved lazily, only when a term actually needs to walk a
relationship or build a query.
"""

from typing import NamedTuple, Optional, Tuple


class Term(NamedTuple):
    """One signed component of a balance formula.

    sign: +1 or -1, exactly as it appears in the owning .balance formula.
    model: the model class name (as a string), resolved lazily via
        _model() below.
    rel: the relationship/backref name on the owning object (Account or
        BankAccount) that this term sums over.
    amount: a tuple of (column_name, sign) pairs identifying the value(s)
        summed per row. Almost every term is a single column, e.g.
        (("amount", 1),). The one exception is BankAccount.balance's
        salaries term, which needs sum(gross_amount) - sum(deduction_amount)
        on the SQL side (see python_attr below for why the Python side
        does NOT also compose this from the two raw columns).
    fk: the column on `model` that points back at the owner - only used
        by the SQL evaluator (sum_via_sql), which has no relationship to
        walk and must filter by the owner's id directly.
    where: equality filters applied to each row before it is summed, e.g.
        (("payment_type", "cash"),). Mirrors an `if row.col == val`
        guard in the original code (Python side) / a `.filter(col == val)`
        clause (SQL side).
    where_sql: EXTRA equality filters applied ONLY by the SQL evaluator,
        on top of `where`. This exists to preserve a real, pre-existing
        asymmetry rather than to smooth it over: BankAccount.balance's
        sales-returns term summed the backref UNFILTERED, while
        bank_account_balance_as_of()'s SQL for the same term has always
        carried `SalesReturn.method == "bank"`. Both were written on the
        assumption that bank_account_id is only ever set for method ==
        "bank", so on conforming data they agree - but on a row that
        breaks that assumption they do NOT, and unifying them would
        change what BankAccount.balance returns. Preserved verbatim here;
        see the note in that Term's comment.
    or_zero: when True, each row's amount is treated as `(value or 0)`
        before summing - mirrors an original `(row.col or 0)` in the
        Python code, for columns that can be NULL (StockPurchase.cost,
        ProductPurchase.total_cost).
    python_attr: when set, the Python-relationship evaluator
        (sum_via_relationships) reads this attribute/property directly
        off each row instead of composing a value from `amount`. Used
        only for BankAccount.balance's salaries term: SalaryPayment.net_paid
        is a Python property that ROUNDS gross - deduction to 2 decimals
        PER ROW (see SalaryPayment.net_paid in models.py), which is not
        bit-for-bit the same as summing the two raw columns unrounded and
        rounding once at the end (what the SQL side does, and always has
        done - see bank_account_balance_as_of()). Composing this term from
        `amount` on the Python side would risk a paisa-level drift from
        the figure BankAccount.balance has always produced, so the
        Python side keeps calling the actual property, verbatim.
    comment: the explanatory comment that accompanied this term in the
        original code, if any - preserved here rather than lost in the
        move from inline code to data (see the module docstrings this was
        transcribed from for the fuller versions of some of these).
    """

    sign: int
    model: str
    rel: str
    amount: Tuple[Tuple[str, int], ...] = (("amount", 1),)
    fk: Optional[str] = None
    where: Tuple[Tuple[str, object], ...] = ()
    where_sql: Tuple[Tuple[str, object], ...] = ()
    or_zero: bool = False
    python_attr: Optional[str] = None
    comment: Optional[str] = None


def _model(name):
    """Resolve a model class by name, lazily - see the module docstring
    for why this can't be a module-level import."""
    import models

    return getattr(models, name)


# --------------------------------------------------------------------------
# Account.balance's terms (models.py ~444). 12 Term entries here; the
# opening_balance that precedes them in the formula is not itself a term
# in this table - see the "opening balance" note in each call site, which
# keeps handling it exactly as it always has (rule 3 of the refactor this
# table came out of: the three functions' opening-balance handling
# differs between them on purpose and must not be unified).
ACCOUNT_TERMS = (
    Term(
        sign=+1,
        model="CreditGiven",
        rel="credit_entries",
        fk="account_id",
    ),
    Term(
        sign=-1,
        model="Receipt",
        rel="receipts",
        fk="account_id",
    ),
    Term(
        sign=-1,
        model="StockPurchase",
        rel="stock_purchases",
        amount=(("cost", 1),),
        fk="account_id",
        where=(("payment_type", "credit"),),
        or_zero=True,
    ),
    Term(
        sign=+1,
        model="SupplierPayment",
        rel="supplier_payments",
        fk="account_id",
    ),
    Term(
        sign=+1,
        model="EmployeeLoan",
        rel="employee_loans",
        fk="account_id",
    ),
    Term(
        sign=-1,
        model="SalaryPayment",
        rel="salary_payments",
        amount=(("deduction_amount", 1),),
        fk="account_id",
        comment=(
            "Only the deducted portion of a salary touches this balance - "
            "the part actually handed over is pay, not a change in what's "
            "owed."
        ),
    ),
    Term(
        sign=-1,
        model="SalesReturn",
        rel="sales_returns",
        fk="account_id",
        comment=(
            'A sales return refunded "on the customer\'s account" (method == '
            '"credit") reduces what they owe, the same direction as a receipt - '
            "account_id is only ever set on a SalesReturn for that method, so "
            "every row in this backref already qualifies."
        ),
    ),
    Term(
        sign=+1,
        model="ProductSale",
        rel="product_sales",
        fk="account_id",
        where=(("method", "credit"),),
        comment=(
            'A non-fuel sale "on the customer\'s account" (method == "credit") '
            "increases what they owe, the same direction as credit_given_total "
            "above - account_id is only ever set on a ProductSale for that "
            "method."
        ),
    ),
    Term(
        sign=-1,
        model="ProductPurchase",
        rel="product_purchases",
        amount=(("total_cost", 1),),
        fk="account_id",
        where=(("payment_type", "credit"),),
        or_zero=True,
        comment=(
            'A product purchase "on credit" (payment_type == "credit") '
            "increases what the pump owes a supplier, the same direction as "
            "purchases_credit_total; total_cost already carries the sign for a "
            "return-to-supplier (see ProductPurchase's docstring in models.py), "
            "so no special case is needed for that here either."
        ),
    ),
    Term(
        sign=+1,
        model="OtherIncome",
        rel="other_income_entries",
        fk="account_id",
        where=(("method", "credit"),),
        comment=(
            'Other Income recorded "on account" (method == "credit") increases '
            "what this account owes, the same direction as "
            "product_sales_credit_total above - account_id is only ever set on "
            "an OtherIncome row for that method."
        ),
    ),
    Term(
        sign=-1,
        model="TankerDeal",
        rel="tanker_purchases",
        amount=(("purchase_cost", 1),),
        fk="supplier_account_id",
        comment=(
            "A pass-through tanker deal (see TankerDeal) can touch this "
            "account from EITHER side, and the two must never cross - hence "
            "the two distinct backrefs. A tanker bought on credit is money "
            "the pump owes this supplier, exactly the same direction as "
            "purchases_credit_total above; a tanker sold on credit is money "
            "this customer owes the pump, exactly the same direction as "
            "credit_given_total. supplier_account_id/customer_account_id are "
            "only ever set for their own \"credit\" payment type, so every row "
            "in either backref already qualifies with no filter needed - same "
            "convention as product_sales_credit_total above."
        ),
    ),
    Term(
        sign=+1,
        model="TankerDeal",
        rel="tanker_sales",
        amount=(("sale_amount", 1),),
        fk="customer_account_id",
    ),
)


# --------------------------------------------------------------------------
# BankAccount.balance's terms (models.py ~1006) / bank_account_balance_as_of's
# terms (ledger_logic.py ~2479) - the SAME 14 Term entries, transcribed once
# here and read by both the Python-relationship evaluator and the SQL
# evaluator. The opening_balance that precedes them is handled at each call
# site, not in this table, because the two sites gate it differently on
# purpose (see rule 3 / each call site's own comment).
BANK_TERMS = (
    Term(
        sign=+1,
        model="BankSale",
        rel="bank_sales",
        fk="bank_account_id",
    ),
    Term(
        sign=+1,
        model="CashDeposit",
        rel="deposits",
        fk="bank_account_id",
    ),
    Term(
        sign=+1,
        model="Receipt",
        rel="receipts",
        fk="bank_account_id",
    ),
    Term(
        sign=-1,
        model="EmployeeLoan",
        rel="employee_loans_paid",
        fk="bank_account_id",
    ),
    Term(
        sign=-1,
        model="Expense",
        rel="expenses",
        fk="bank_account_id",
    ),
    Term(
        sign=-1,
        model="StockPurchase",
        rel="fuel_purchases",
        amount=(("cost", 1),),
        fk="bank_account_id",
        where=(("payment_type", "cash"),),
        or_zero=True,
    ),
    Term(
        sign=-1,
        model="SupplierPayment",
        rel="supplier_payments_paid",
        fk="bank_account_id",
    ),
    Term(
        sign=-1,
        model="SalaryPayment",
        rel="salary_payments_paid",
        amount=(("gross_amount", 1), ("deduction_amount", -1)),
        fk="bank_account_id",
        python_attr="net_paid",
        comment=(
            "Only the net handed over leaves the bank - the deducted portion "
            "of a salary never moves as money, it just settles an advance."
        ),
    ),
    Term(
        sign=-1,
        model="SalesReturn",
        rel="sales_returns",
        fk="bank_account_id",
        where_sql=(("method", "bank"),),
        comment=(
            'A sales return refunded out of this bank (method == "bank") '
            "leaves the same way a loan/expense/purchase paid via this bank "
            "does - bank_account_id is only ever set on a SalesReturn for "
            "that method, so every row in this backref already qualifies. "
            "NOTE the filter is where_sql, not where, and that is not a "
            "tidy-up waiting to happen: the original Python property summed "
            "this backref with NO method filter while the original SQL "
            "carried one. On a row that breaks the assumption above (a "
            "bank_account_id set with method != 'bank') the two therefore "
            "disagree - verified: 500.0 vs 1000.0 on such a row. That is a "
            "pre-existing inconsistency, preserved deliberately so this "
            "refactor changes no behaviour. Fixing it is a separate, "
            "deliberate decision - see also the per-row rounding asymmetry "
            "documented on python_attr above."
        ),
    ),
    Term(
        sign=+1,
        model="ProductSale",
        rel="product_sales",
        fk="bank_account_id",
        where=(("method", "bank"),),
        comment=(
            'bank_account_id is only ever set on a ProductSale for method == '
            '"bank" (a non-fuel sale received into this bank), and on a '
            'ProductPurchase for a cash-paid (payment_type == "cash") delivery '
            "settled via this bank - the payment_type check below mirrors "
            "fuel_purchases_total's above, and total_cost's sign already "
            "handles a return-to-supplier with no special case needed."
        ),
    ),
    Term(
        sign=-1,
        model="ProductPurchase",
        rel="product_purchases",
        amount=(("total_cost", 1),),
        fk="bank_account_id",
        where=(("payment_type", "cash"),),
        or_zero=True,
    ),
    Term(
        sign=+1,
        model="OtherIncome",
        rel="other_income_entries",
        fk="bank_account_id",
        comment=(
            'other_income_entries is only ever populated with method == "bank" '
            "rows for this bank account (see OtherIncome and "
            "ledger_other_income() in app.py) - no method filter needed here, "
            "same reasoning as product_sales_total's docstring above it."
        ),
    ),
    Term(
        sign=-1,
        model="TankerDeal",
        rel="tanker_purchases_paid",
        amount=(("purchase_cost", 1),),
        fk="purchase_bank_account_id",
        comment=(
            "Bank-method sides of a pass-through tanker deal (see TankerDeal). "
            "purchase_bank_account_id / sale_bank_account_id are only ever set "
            'for their own side\'s "bank" payment type, so neither backref '
            "needs a filter - same reasoning other_income_total above uses. "
            "The two are separate backrefs, not one shared list, so a deal "
            "whose purchase and sale both route through THIS bank still nets "
            "correctly (out by cost, in by sale) rather than counting once."
        ),
    ),
    Term(
        sign=+1,
        model="TankerDeal",
        rel="tanker_sales_received",
        amount=(("sale_amount", 1),),
        fk="sale_bank_account_id",
    ),
)


def _passes_where(row, where):
    return all(getattr(row, col) == val for col, val in where)


def _row_value(row, term):
    if term.python_attr is not None:
        return getattr(row, term.python_attr)
    value = 0
    for col, col_sign in term.amount:
        v = getattr(row, col)
        if term.or_zero:
            v = v or 0
        value += v * col_sign
    return value


def sum_via_relationships(owner, terms, start=0):
    """What Account.balance and BankAccount.balance use: sums the
    RELATIONSHIP backrefs in Python, exactly as the original inline code
    did. Deliberately NOT switched to SQL - that would change when the
    value is computed relative to the session's flush state, which is a
    behaviour change even on the rows where it happens to produce the
    same number.

    Each term is summed with Python's builtin sum() over its own rows
    first - exactly mirroring the original code's
    `sum(row.amount for row in owner.rel)` pattern - and only then
    combined with the other terms and `start` (typically the owner's
    opening_balance) in the same left-to-right order the original
    `opening + t1 - t2 + t3 ...` expression used. This isn't pedantry:
    float addition isn't associative, so accumulating row-by-row across
    terms instead of term-by-term can shift a result in its last bit,
    which round(..., 2) usually - but not always - absorbs.

    Returns the raw (unrounded) signed sum - callers round(..., 2) once,
    same as the original code did.
    """
    total = start
    for term in terms:
        rows = getattr(owner, term.rel)
        term_total = sum(
            _row_value(row, term) for row in rows if _passes_where(row, term.where)
        )
        total += term.sign * term_total
    return total


def eager_load(query, model, terms, via=None):
    """Fetch every relationship `terms` walks up front, one query per
    relationship for the WHOLE result set, instead of one query per
    relationship per row.

    sum_via_relationships() reads `owner.<rel>`; if that collection is
    already loaded it costs nothing, and if it isn't, SQLAlchemy emits a
    query right there. Listing N accounts therefore cost 12N queries
    (14N for bank accounts) purely in lazy loads. With this it is 12 (or
    14), whatever N is.

    This changes only WHEN the rows are fetched, never which rows or how
    they are summed - the arithmetic still runs in Python over the same
    collections, in the same term order, so the result is identical down
    to the last bit. selectinload (a second SELECT ... WHERE fk IN (...))
    is used rather than a join, so no row is duplicated by fanout and
    each collection holds exactly what a lazy load would have put there.

    `via` nests the loads under another relationship, for the Accounts
    list: a parent row shows group_balance, which sums each child's
    balance too, so the children's own term relationships have to come
    across as well or the N+1 simply moves down a level.
    """
    from sqlalchemy.orm import selectinload

    seen = []
    for term in terms:
        if term.rel not in seen:
            seen.append(term.rel)
    if via is None:
        opts = [selectinload(getattr(model, rel)) for rel in seen]
    else:
        opts = [selectinload(via).selectinload(getattr(model, rel))
                for rel in seen]
    return query.options(*opts)


def sum_via_sql(owner_id, terms, date_column=None, as_of=None, start=0.0):
    """What bank_account_balance_as_of() uses: one explicit SQL sum per
    term, filtered by the owner's id and (optionally) by
    `date_column <= as_of`. Every sum is coalesced to 0, matching the
    original explicit SQL (SQLAlchemy's SUM returns NULL over zero rows,
    where Python's sum() of an empty sequence is already 0).

    Terms are combined with `start` (typically the gated opening balance)
    in the same left-to-right order the original
    `opening + t1_total + t2_total + ...` expression used - see
    sum_via_relationships' docstring for why that order is preserved
    deliberately rather than accumulated some other, equally-valid way.

    Returns the raw (unrounded) signed sum - callers round(..., 2) once,
    same as the original code did.
    """
    from sqlalchemy import func

    from extensions import db

    total = start
    for term in terms:
        model = _model(term.model)
        expr = None
        for col, col_sign in term.amount:
            column = getattr(model, col)
            piece = column if col_sign == 1 else -column
            expr = piece if expr is None else expr + piece
        query = db.session.query(func.coalesce(func.sum(expr), 0)).filter(
            getattr(model, term.fk) == owner_id
        )
        for col, val in tuple(term.where) + tuple(term.where_sql):
            query = query.filter(getattr(model, col) == val)
        if date_column is not None and as_of is not None:
            query = query.filter(getattr(model, date_column) <= as_of)
        total += term.sign * query.scalar()
    return total
