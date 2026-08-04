from datetime import datetime

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from extensions import db


class User(UserMixin, db.Model):
    """A login. Deactivating rather than deleting is deliberate: every
    ledger row records which user entered it (user_id), so a user who has
    ever recorded anything has to stay resolvable for that history to
    still read correctly."""

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    display_name = db.Column(db.String(120))
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False)  # "owner" or "staff"
    is_active_user = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=datetime.now)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def is_owner(self):
        return self.role == "owner"

    @property
    def label(self):
        return self.display_name or self.username


class Shift(db.Model):
    """A named working period within a day (e.g. Morning/Evening/Night).

    A single "Full Day" shift is seeded automatically so a pump that
    doesn't split its day never has to think about shifts at all - every
    reading, credit sale, and bank sale just lands in that one shift, and
    the Ledger hides the selector entirely while only one active shift
    exists. Adding more shifts later doesn't disturb existing rows."""

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(60), nullable=False, unique=True)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=datetime.now)


class FuelType(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)
    price_per_liter = db.Column(db.Float, nullable=False, default=0)


class FuelPriceHistory(db.Model):
    """One row per price change, effective from effective_date onward until
    the next row for the same fuel type. FuelType.price_per_liter is kept
    as a denormalized "current price" cache; this table is the source of
    truth for what a fuel cost on any given past date, so correcting an
    old Sale/CreditGiven entry can re-price at the rate that was actually
    in effect then instead of today's rate (see ledger_logic.price_on_date)."""

    id = db.Column(db.Integer, primary_key=True)
    fuel_type_id = db.Column(db.Integer, db.ForeignKey("fuel_type.id"), nullable=False)
    price_per_liter = db.Column(db.Float, nullable=False)
    effective_date = db.Column(db.Date, nullable=False)
    recorded_at = db.Column(db.DateTime, default=datetime.now)

    fuel_type = db.relationship("FuelType", backref="price_history")


class Tank(db.Model):
    """A physical storage tank. Several tanks can share a fuel type.

    Stock is never stored as a mutable counter here - it's always
    calculated from starting_stock_liters + purchases - sales, filtered to
    a date, so backfilling or editing a past ledger entry can never leave
    stock out of sync (see book_stock() in ledger_logic.py).

    starting_stock_liters is the level at the START of starting_stock_date
    (equivalently, the end of the day before it) - not "since the
    beginning of time". This mirrors Account/BankAccount/CashAccount's
    opening_balance + opening_balance_date pattern, so a tank set up with
    today's physical stock can still be backfilled with months of older
    purchases/sales without every historical figure double-counting them.

    starting_stock_date NULL means "treat as the beginning of time" - the
    same meaning an unset date carries for those other opening balances,
    and exactly the behaviour this column didn't used to have a choice
    about, so existing installations (every tank created before this
    column existed) are unaffected.
    """

    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.Integer, nullable=False, unique=True)
    fuel_type_id = db.Column(db.Integer, db.ForeignKey("fuel_type.id"), nullable=False)
    capacity_liters = db.Column(db.Float, nullable=False)
    starting_stock_liters = db.Column(db.Float, nullable=False, default=0)
    starting_stock_date = db.Column(db.Date, nullable=True)
    low_stock_threshold = db.Column(db.Float, nullable=False, default=0)

    fuel_type = db.relationship("FuelType")

    @property
    def label(self):
        return f"Tank {self.number} - {self.fuel_type.name}"


class Dispenser(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.Integer, nullable=False, unique=True)

    nozzles = db.relationship(
        "Nozzle", backref="dispenser", order_by="Nozzle.nozzle_number"
    )


class Nozzle(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    dispenser_id = db.Column(db.Integer, db.ForeignKey("dispenser.id"), nullable=False)
    nozzle_number = db.Column(db.Integer, nullable=False)
    tank_id = db.Column(db.Integer, db.ForeignKey("tank.id"), nullable=False)

    tank = db.relationship("Tank")

    @property
    def fuel_type(self):
        return self.tank.fuel_type

    @property
    def label(self):
        return f"Dispenser {self.dispenser.number} - Nozzle {self.nozzle_number}"


class NozzleReset(db.Model):
    """Marks that a nozzle's physical meter was replaced/rolled over as of
    reset_date - readings from that date onward start a fresh counting era.
    previous_reading_for/nearest_earlier_reading/next_sale_on_or_after in
    ledger_logic.py all stop enforcing continuity across this boundary, so
    a lower reading right after a reset isn't rejected as an error."""

    id = db.Column(db.Integer, primary_key=True)
    nozzle_id = db.Column(db.Integer, db.ForeignKey("nozzle.id"), nullable=False)
    reset_date = db.Column(db.Date, nullable=False)
    note = db.Column(db.String(200))
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    recorded_at = db.Column(db.DateTime, default=datetime.now)

    nozzle = db.relationship("Nozzle", backref="resets")
    user = db.relationship("User")


class Account(db.Model):
    """A single ledger shared by any party the pump does business with -
    customer, supplier, employee, or any combination (e.g. another pump
    that sometimes buys fuel and sometimes sells it back). account_type is
    a plain label for organizing/searching; it never restricts which kind
    of entry (CreditGiven, StockPurchase, EmployeeLoan, etc.) can be
    posted against this account - all six entry tables point at the same
    Account row via account_id, so one account can accumulate both
    "customer" and "supplier" style entries in one running balance.
    """

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    phone = db.Column(db.String(30))
    account_type = db.Column(db.String(20), nullable=False, default="customer")  # customer | supplier | employee
    opening_balance = db.Column(db.Float, nullable=False, default=0)
    opening_balance_date = db.Column(db.Date, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.now)

    @property
    def balance(self):
        """Positive balance = this account owes the pump money (debitor).
        Negative balance = the pump owes this account money (creditor)."""
        credit_given_total = sum(c.amount for c in self.credit_entries)
        receipts_total = sum(r.amount for r in self.receipts)
        purchases_credit_total = sum(
            (p.cost or 0) for p in self.stock_purchases if p.payment_type == "credit"
        )
        supplier_payments_total = sum(p.amount for p in self.supplier_payments)
        loans_total = sum(l.amount for l in self.employee_loans)
        # Only the deducted portion of a salary touches this balance - the
        # part actually handed over is pay, not a change in what's owed.
        salary_deductions_total = sum(s.deduction_amount for s in self.salary_payments)
        # A sales return refunded "on the customer's account" (method ==
        # "credit") reduces what they owe, the same direction as a receipt -
        # account_id is only ever set on a SalesReturn for that method, so
        # every row in this backref already qualifies.
        sales_returns_total = sum(sr.amount for sr in self.sales_returns)
        # A non-fuel sale "on the customer's account" (method == "credit")
        # increases what they owe, the same direction as credit_given_total
        # above - account_id is only ever set on a ProductSale for that
        # method. A product purchase "on credit" (payment_type == "credit")
        # increases what the pump owes a supplier, the same direction as
        # purchases_credit_total; total_cost already carries the sign for a
        # return-to-supplier (see ProductPurchase's docstring in models.py),
        # so no special case is needed for that here either.
        product_sales_credit_total = sum(ps.amount for ps in self.product_sales if ps.method == "credit")
        product_purchases_credit_total = sum(
            (pp.total_cost or 0) for pp in self.product_purchases if pp.payment_type == "credit"
        )
        return round(
            self.opening_balance
            + credit_given_total
            - receipts_total
            - purchases_credit_total
            + supplier_payments_total
            + loans_total
            - salary_deductions_total
            - sales_returns_total
            + product_sales_credit_total
            - product_purchases_credit_total,
            2,
        )


class Sale(db.Model):
    """A debit-side ledger entry: one nozzle's meter reading on one date,
    within one shift.

    At most one Sale exists per (nozzle_id, entry_date, shift_id) -
    re-submitting a reading for a date/shift that already has one updates
    it in place rather than creating a duplicate, which is what makes
    backfilling/editing a past date safe. A pump that doesn't split its
    day just has every row land in the single seeded "Full Day" shift, so
    the constraint behaves exactly like one-per-nozzle-per-day.

    testing_liters is fuel run through the nozzle to test it (e.g. after
    maintenance) rather than sold - it must not be billed, and because it
    physically stays at the pump and drains back into the same tank, it
    must not be treated as gone from stock either. The invariant that
    ties it to the meter is:

        liters + testing_liters == current_reading - previous_reading

    liters itself stays the NET figure - what actually left the tank for
    good - not the raw meter difference. That's deliberate: every other
    consumer of Sale.liters (book_stock(), sales_breakdown_for_date(),
    fuel_sales_for_date(), COGS, trends, exports) already means "the sale"
    by it, and net-sold really is the quantity that permanently left the
    tank, so none of them need to change to account for testing.
    """

    __table_args__ = (
        db.UniqueConstraint("nozzle_id", "entry_date", "shift_id", name="uq_sale_nozzle_date_shift"),
    )

    id = db.Column(db.Integer, primary_key=True)
    nozzle_id = db.Column(db.Integer, db.ForeignKey("nozzle.id"), nullable=False)
    shift_id = db.Column(db.Integer, db.ForeignKey("shift.id"), nullable=False)
    entry_date = db.Column(db.Date, nullable=False)
    previous_reading = db.Column(db.Float, nullable=False)
    current_reading = db.Column(db.Float, nullable=False)
    liters = db.Column(db.Float, nullable=False)
    testing_liters = db.Column(db.Float, nullable=False, default=0)
    price_per_liter = db.Column(db.Float, nullable=False)
    total_amount = db.Column(db.Float, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    recorded_at = db.Column(db.DateTime, default=datetime.now)

    nozzle = db.relationship("Nozzle", backref="sales")
    shift = db.relationship("Shift")
    user = db.relationship("User")


class CreditGiven(db.Model):
    """A credit-side ledger entry: fuel already counted in a nozzle's Sale
    that was handed to a customer on account instead of collected as cash.

    This does NOT touch tank stock (the Sale entry already accounted for
    the liters leaving the tank) - it only moves that amount of revenue
    from "cash collected" onto the customer's running balance.
    """

    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey("account.id"), nullable=False)
    fuel_type_id = db.Column(db.Integer, db.ForeignKey("fuel_type.id"), nullable=False)
    shift_id = db.Column(db.Integer, db.ForeignKey("shift.id"), nullable=False)
    entry_date = db.Column(db.Date, nullable=False)
    liters = db.Column(db.Float, nullable=False)
    price_per_liter = db.Column(db.Float, nullable=False)
    amount = db.Column(db.Float, nullable=False)
    vehicle_number = db.Column(db.String(30))
    note = db.Column(db.String(200))
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    recorded_at = db.Column(db.DateTime, default=datetime.now)

    account = db.relationship("Account", backref="credit_entries")
    fuel_type = db.relationship("FuelType")
    shift = db.relationship("Shift")
    user = db.relationship("User")


class SalesReturn(db.Model):
    """A debit-side reversal: fuel a customer physically brings back into
    a tank, refunded to them - distinct from Sale.testing_liters, which
    never involved a customer at all. Stock comes back IN, the same
    direction as a StockPurchase (see book_stock() in ledger_logic.py),
    and the refund goes out as cash, out of a bank, or as a reduction of
    what a credit customer owes (method == "credit", the same "on
    account" idea CreditGiven uses in reverse).

    Priced with price_on_date() for its own entry_date, same as any other
    sale, so a return of fuel bought weeks ago refunds the rate that was
    actually charged then, not today's rate.

    Fuel-only for now - non-fuel products (lubricants, etc.) arrive in a
    later phase and aren't sold through a nozzle/tank at all, so this
    model should grow to cover them then rather than being reused as-is.
    """

    id = db.Column(db.Integer, primary_key=True)
    entry_date = db.Column(db.Date, nullable=False)
    shift_id = db.Column(db.Integer, db.ForeignKey("shift.id"), nullable=False)
    fuel_type_id = db.Column(db.Integer, db.ForeignKey("fuel_type.id"), nullable=False)
    tank_id = db.Column(db.Integer, db.ForeignKey("tank.id"), nullable=False)
    liters = db.Column(db.Float, nullable=False)
    price_per_liter = db.Column(db.Float, nullable=False)
    amount = db.Column(db.Float, nullable=False)
    method = db.Column(db.String(10), nullable=False, default="cash")  # cash | bank | credit
    bank_account_id = db.Column(db.Integer, db.ForeignKey("bank_account.id"), nullable=True)
    account_id = db.Column(db.Integer, db.ForeignKey("account.id"), nullable=True)
    note = db.Column(db.String(200))
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    recorded_at = db.Column(db.DateTime, default=datetime.now)

    shift = db.relationship("Shift")
    fuel_type = db.relationship("FuelType")
    tank = db.relationship("Tank", backref="sales_returns")
    bank_account = db.relationship("BankAccount", backref="sales_returns")
    account = db.relationship("Account", backref="sales_returns")
    user = db.relationship("User")


class StockPurchase(db.Model):
    """payment_type decides whether this purchase touches an account at
    all: "credit" ties it to a supplier account (via account_id) and
    never touches cash/bank at purchase time; "cash" means it was paid
    for immediately, and method/bank_account_id (same pattern as Receipt,
    EmployeeLoan, Expense) decide whether that payment came out of
    cash-in-hand or a specific bank account. method/bank_account_id are
    only meaningful when payment_type == "cash"."""

    id = db.Column(db.Integer, primary_key=True)
    tank_id = db.Column(db.Integer, db.ForeignKey("tank.id"), nullable=False)
    entry_date = db.Column(db.Date, nullable=False)
    liters = db.Column(db.Float, nullable=False)
    cost = db.Column(db.Float)
    payment_type = db.Column(db.String(10), nullable=False, default="cash")  # cash | credit
    method = db.Column(db.String(10), nullable=False, default="cash")  # cash | bank (only when payment_type == cash)
    bank_account_id = db.Column(db.Integer, db.ForeignKey("bank_account.id"), nullable=True)
    account_id = db.Column(db.Integer, db.ForeignKey("account.id"), nullable=True)
    note = db.Column(db.String(200))
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    recorded_at = db.Column(db.DateTime, default=datetime.now)

    tank = db.relationship("Tank", backref="purchases")
    account = db.relationship("Account", backref="stock_purchases")
    bank_account = db.relationship("BankAccount", backref="fuel_purchases")
    user = db.relationship("User")


class SupplierPayment(db.Model):
    """A credit-side ledger entry: money we paid an account against fuel
    we previously took on credit. Mirrors Receipt - reduces what we owe
    the account the same way a receipt reduces what a customer owes us.
    Paid as cash (reduces cash-in-hand) or from a specific bank account
    (reduces that bank's balance) - same "Paid via" pattern as Receipt,
    EmployeeLoan, and Expense."""

    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey("account.id"), nullable=False)
    entry_date = db.Column(db.Date, nullable=False)
    amount = db.Column(db.Float, nullable=False)
    method = db.Column(db.String(10), nullable=False, default="cash")  # cash | bank
    bank_account_id = db.Column(db.Integer, db.ForeignKey("bank_account.id"), nullable=True)
    note = db.Column(db.String(200))
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    recorded_at = db.Column(db.DateTime, default=datetime.now)

    account = db.relationship("Account", backref="supplier_payments")
    bank_account = db.relationship("BankAccount", backref="supplier_payments_paid")
    user = db.relationship("User")


class Receipt(db.Model):
    """A debit-side ledger entry: money received from any account, whether
    that's a customer paying down what they owe or an employee repaying a
    loan - account_type doesn't matter, this is one merged entry kind
    covering both. Optionally routed straight into a bank account instead
    of being received as cash (method + bank_account_id); when method is
    "cash" the amount adds to cash-in-hand instead."""

    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey("account.id"), nullable=False)
    entry_date = db.Column(db.Date, nullable=False)
    amount = db.Column(db.Float, nullable=False)
    method = db.Column(db.String(10), nullable=False, default="cash")  # cash | bank
    bank_account_id = db.Column(db.Integer, db.ForeignKey("bank_account.id"), nullable=True)
    note = db.Column(db.String(200))
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    recorded_at = db.Column(db.DateTime, default=datetime.now)

    account = db.relationship("Account", backref="receipts")
    bank_account = db.relationship("BankAccount", backref="receipts")
    user = db.relationship("User")


class EmployeeLoan(db.Model):
    """A credit-side ledger entry: a loan or advance given to an account,
    the same way CreditGiven works for customers - it increases what the
    account owes the pump. Paid out as cash (reduces cash-in-hand) or from
    a specific bank account (reduces that bank's balance)."""

    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey("account.id"), nullable=False)
    entry_date = db.Column(db.Date, nullable=False)
    amount = db.Column(db.Float, nullable=False)
    method = db.Column(db.String(10), nullable=False, default="cash")  # cash | bank
    bank_account_id = db.Column(db.Integer, db.ForeignKey("bank_account.id"), nullable=True)
    note = db.Column(db.String(200))
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    recorded_at = db.Column(db.DateTime, default=datetime.now)

    account = db.relationship("Account", backref="employee_loans")
    bank_account = db.relationship("BankAccount", backref="employee_loans_paid")
    user = db.relationship("User")


class Expense(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    entry_date = db.Column(db.Date, nullable=False)
    category = db.Column(db.String(80), nullable=False)
    description = db.Column(db.String(200))
    amount = db.Column(db.Float, nullable=False)
    method = db.Column(db.String(10), nullable=False, default="cash")  # cash | bank
    bank_account_id = db.Column(db.Integer, db.ForeignKey("bank_account.id"), nullable=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    recorded_at = db.Column(db.DateTime, default=datetime.now)

    bank_account = db.relationship("BankAccount", backref="expenses")
    user = db.relationship("User")


class BankAccount(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, unique=True)
    opening_balance = db.Column(db.Float, nullable=False, default=0)
    opening_balance_date = db.Column(db.Date, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.now)

    @property
    def balance(self):
        sales_total = sum(s.amount for s in self.bank_sales)
        deposits_total = sum(d.amount for d in self.deposits)
        receipts_total = sum(r.amount for r in self.receipts)
        loans_total = sum(l.amount for l in self.employee_loans_paid)
        expenses_total = sum(e.amount for e in self.expenses)
        fuel_purchases_total = sum(
            (p.cost or 0) for p in self.fuel_purchases if p.payment_type == "cash"
        )
        supplier_payments_total = sum(p.amount for p in self.supplier_payments_paid)
        # Only the net handed over leaves the bank - the deducted portion
        # of a salary never moves as money, it just settles an advance.
        salaries_total = sum(s.net_paid for s in self.salary_payments_paid)
        # A sales return refunded out of this bank (method == "bank")
        # leaves the same way a loan/expense/purchase paid via this bank
        # does - bank_account_id is only ever set on a SalesReturn for
        # that method, so every row in this backref already qualifies.
        sales_returns_total = sum(sr.amount for sr in self.sales_returns)
        # bank_account_id is only ever set on a ProductSale for method ==
        # "bank" (a non-fuel sale received into this bank), and on a
        # ProductPurchase for a cash-paid (payment_type == "cash") delivery
        # settled via this bank - the payment_type check below mirrors
        # fuel_purchases_total's above, and total_cost's sign already
        # handles a return-to-supplier with no special case needed.
        product_sales_total = sum(ps.amount for ps in self.product_sales if ps.method == "bank")
        product_purchases_total = sum(
            (pp.total_cost or 0) for pp in self.product_purchases if pp.payment_type == "cash"
        )
        return round(
            self.opening_balance
            + sales_total
            + deposits_total
            + receipts_total
            - loans_total
            - expenses_total
            - fuel_purchases_total
            - supplier_payments_total
            - salaries_total
            - sales_returns_total
            + product_sales_total
            - product_purchases_total,
            2,
        )


class BankSale(db.Model):
    """A debit-side ledger entry: the portion of a date's nozzle sales that
    was actually collected via card/bank rather than cash, reconciled
    against a specific bank account's statement. Doesn't touch tank stock
    or Sale - it's a payment-method reclassification, the same way
    CreditGiven reclassifies revenue onto a customer's balance instead of
    cash."""

    id = db.Column(db.Integer, primary_key=True)
    bank_account_id = db.Column(db.Integer, db.ForeignKey("bank_account.id"), nullable=False)
    shift_id = db.Column(db.Integer, db.ForeignKey("shift.id"), nullable=False)
    entry_date = db.Column(db.Date, nullable=False)
    amount = db.Column(db.Float, nullable=False)
    note = db.Column(db.String(200))
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    recorded_at = db.Column(db.DateTime, default=datetime.now)

    bank_account = db.relationship("BankAccount", backref="bank_sales")
    shift = db.relationship("Shift")
    user = db.relationship("User")


class CashDeposit(db.Model):
    """A credit-side ledger entry: cash physically deposited into a bank
    account - increases that bank account's balance, decreases cash in
    hand."""

    id = db.Column(db.Integer, primary_key=True)
    bank_account_id = db.Column(db.Integer, db.ForeignKey("bank_account.id"), nullable=False)
    entry_date = db.Column(db.Date, nullable=False)
    amount = db.Column(db.Float, nullable=False)
    note = db.Column(db.String(200))
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    recorded_at = db.Column(db.DateTime, default=datetime.now)

    bank_account = db.relationship("BankAccount", backref="deposits")
    user = db.relationship("User")


class CashAccount(db.Model):
    """Singleton row representing cash-in-hand. Its balance is computed in
    ledger_logic.cash_account_balance() since it depends on Sale,
    CreditGiven and BankSale totals rather than a simple backref sum."""

    id = db.Column(db.Integer, primary_key=True)
    opening_balance = db.Column(db.Float, nullable=False, default=0)
    opening_balance_date = db.Column(db.Date, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.now)


class TankDip(db.Model):
    """A physical dip measurement for one tank on one date, compared
    against calculated book stock. At most one dip per (tank_id, entry_date).

    dip_liters is always the figure compared against book stock. A dip
    stick physically measures depth, not volume, so when the tank has a
    calibration chart (TankDipChart) the staff enter dip_cm and the liters
    are interpolated from that chart; dip_cm is then kept alongside as the
    raw measurement actually taken. Tanks without a chart keep entering
    liters directly and leave dip_cm empty."""

    __table_args__ = (db.UniqueConstraint("tank_id", "entry_date", name="uq_tankdip_tank_date"),)

    id = db.Column(db.Integer, primary_key=True)
    tank_id = db.Column(db.Integer, db.ForeignKey("tank.id"), nullable=False)
    entry_date = db.Column(db.Date, nullable=False)
    dip_cm = db.Column(db.Float, nullable=True)
    dip_liters = db.Column(db.Float, nullable=False)
    # A stick measurement in cm of any water sitting at the bottom of the
    # tank - always cm, regardless of whether the tank's own dip above is
    # taken in cm or liters, since a dip chart doesn't have a water curve.
    # Water displaces fuel (distorting the dip) and damages a customer's
    # engine, so this is tracked as a diagnostic warning only - it never
    # adjusts book stock or any other figure.
    water_cm = db.Column(db.Float, nullable=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    recorded_at = db.Column(db.DateTime, default=datetime.now)

    tank = db.relationship("Tank", backref="dips")
    user = db.relationship("User")


class TankDipChart(db.Model):
    """One row of a tank's calibration table: at depth_cm of fuel, the tank
    holds liters. Tank-specific because it depends on physical shape.
    ledger_logic.liters_from_dip_cm() linearly interpolates between the two
    nearest rows, so the chart doesn't need a row for every millimetre."""

    __table_args__ = (
        db.UniqueConstraint("tank_id", "depth_cm", name="uq_dipchart_tank_depth"),
    )

    id = db.Column(db.Integer, primary_key=True)
    tank_id = db.Column(db.Integer, db.ForeignKey("tank.id"), nullable=False)
    depth_cm = db.Column(db.Float, nullable=False)
    liters = db.Column(db.Float, nullable=False)

    tank = db.relationship("Tank", backref="dip_chart_rows")


class CashHandover(db.Model):
    """What was physically counted at the end of a shift, against what the
    ledger says should have been collected in cash for that shift.

    Deliberately NOT a money movement: cash-in-hand is already derived
    from sales/credit/bank-sales, so adding the declared figure on top
    would double-count it. This row exists purely to surface a variance
    (declared - expected) the same way a tank dip surfaces a stock
    variance. A confirmed shortfall the owner decides to absorb or recover
    is recorded separately - as an Expense, or as a loan against the
    attendant's account - which is what actually moves the numbers."""

    __table_args__ = (
        db.UniqueConstraint("entry_date", "shift_id", name="uq_handover_date_shift"),
    )

    id = db.Column(db.Integer, primary_key=True)
    entry_date = db.Column(db.Date, nullable=False)
    shift_id = db.Column(db.Integer, db.ForeignKey("shift.id"), nullable=False)
    attendant_id = db.Column(db.Integer, db.ForeignKey("account.id"), nullable=True)
    declared_amount = db.Column(db.Float, nullable=False)
    note = db.Column(db.String(200))
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    recorded_at = db.Column(db.DateTime, default=datetime.now)

    shift = db.relationship("Shift")
    attendant = db.relationship("Account", backref="handovers")
    user = db.relationship("User")


class SalaryPayment(db.Model):
    """Salary paid to an account for a period, optionally settling part of
    what that account already owes the pump (an earlier advance/loan).

    gross_amount is the full salary earned - that's the figure the P&L
    should carry as a wage cost. deduction_amount is the slice withheld
    against the account's outstanding balance, so it never leaves as money;
    only net_paid (gross - deduction) actually reduces cash or a bank
    account. Splitting it this way means an advance can be recovered
    through payroll without needing a fake "receipt" the employee never
    physically handed over."""

    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey("account.id"), nullable=False)
    entry_date = db.Column(db.Date, nullable=False)
    period_label = db.Column(db.String(40))  # e.g. "Jul 2026"
    gross_amount = db.Column(db.Float, nullable=False)
    deduction_amount = db.Column(db.Float, nullable=False, default=0)
    method = db.Column(db.String(10), nullable=False, default="cash")  # cash | bank
    bank_account_id = db.Column(db.Integer, db.ForeignKey("bank_account.id"), nullable=True)
    note = db.Column(db.String(200))
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    recorded_at = db.Column(db.DateTime, default=datetime.now)

    account = db.relationship("Account", backref="salary_payments")
    bank_account = db.relationship("BankAccount", backref="salary_payments_paid")
    user = db.relationship("User")

    @property
    def net_paid(self):
        return round(self.gross_amount - self.deduction_amount, 2)


class Product(db.Model):
    """A non-fuel item sold at the pump - lubricants, filters, and shop
    items. The pump's real catalogue runs to ~95 SKUs (Shell Helix/Rimula/
    Ultra grades in 1/3/4/10/20 L packs, dozens of vehicle-specific
    filters, brake oil, gear oil, coolant) plus a separate shop line, and
    together they earn dealer commission comparable to fuel itself on a
    fraction of the revenue - which is why this table exists at all.

    category is a plain organizing label, exactly like Account.account_type:
    it never restricts which entry can be posted against a product. It
    exists purely so lubricant/filter/shop profit can be reported
    separately later.

    unit says how the product is COUNTED, not what's printed on the pack -
    lubricants are sold as sealed tins, so they're counted in pieces even
    though the tin holds liters. The real workbook's "AMOUNT SOLD (L)"
    column actually holds unit counts; this field exists so that exact
    confusion can't creep back in here.

    purchase_rate (the indent/dealer rate) and retail_rate are denormalized
    "current rate" caches over ProductRateHistory - the same relationship
    FuelType.price_per_liter has to FuelPriceHistory (see
    product_rates_on_date() in ledger_logic.py).

    opening_stock/opening_stock_date follow Tank.starting_stock_liters/
    starting_stock_date exactly: opening_stock is the level at the START
    of opening_stock_date (equivalently, the end of the day before it),
    and opening_stock_date NULL means "treat as the beginning of time"
    (see product_stock() in ledger_logic.py).

    Deactivated rather than deleted once a product has sale/purchase
    history, the same reasoning as User.is_active_user - a ProductSale/
    ProductPurchase row keeps pointing at product_id, so the product it
    names has to stay resolvable.
    """

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, unique=True)
    # lubricant | filter | shop | other - see class docstring; a case-
    # insensitive unique index isn't portable across SQLite/Postgres, so
    # duplicate matching on name is done in application code instead.
    category = db.Column(db.String(20), nullable=False, default="lubricant")
    pack_size = db.Column(db.String(20), nullable=True)  # free text label ("3 L", "0.7 L", "-") - not a quantity
    unit = db.Column(db.String(10), nullable=False, default="piece")  # piece | litre
    purchase_rate = db.Column(db.Float, nullable=False, default=0)
    retail_rate = db.Column(db.Float, nullable=False, default=0)
    opening_stock = db.Column(db.Float, nullable=False, default=0)
    opening_stock_date = db.Column(db.Date, nullable=True)
    low_stock_threshold = db.Column(db.Float, nullable=False, default=0)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=datetime.now)

    @property
    def commission(self):
        """Dealer margin per unit. Never stored - deriving it from the two
        rates means it can never disagree with them."""
        return round(self.retail_rate - self.purchase_rate, 2)

    @property
    def label(self):
        return f"{self.name} ({self.pack_size})" if self.pack_size else self.name


class ProductRateHistory(db.Model):
    """One row per rate change for a product - mirrors FuelPriceHistory,
    except it carries BOTH the purchase (indent) and retail rate in the
    same row, because in practice an indent-rate change almost always
    arrives together with a new retail rate; splitting them into two
    tables would let them drift apart for the same date.
    Product.purchase_rate/retail_rate are kept as "current rate" caches
    over this table, the same role FuelType.price_per_liter plays over
    FuelPriceHistory (see product_rates_on_date() in ledger_logic.py)."""

    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=False)
    purchase_rate = db.Column(db.Float, nullable=False)
    retail_rate = db.Column(db.Float, nullable=False)
    effective_date = db.Column(db.Date, nullable=False)
    recorded_at = db.Column(db.DateTime, default=datetime.now)

    product = db.relationship("Product", backref="rate_history")


class ProductSale(db.Model):
    """A non-fuel sale line: one product, one shift, one date. Defined now
    so the whole catalogue lands in one migration; the Ledger entry form
    and the cash/bank/account balance wiring for this table are built in
    the next phase, not here.

    Snapshots BOTH purchase_rate and retail_rate at the moment of sale -
    deliberately stronger than Sale/StockPurchase's treatment of fuel,
    where cost is a weighted average over purchase invoices because
    litres are fungible (see weighted_avg_cost() in ledger_logic.py). A
    product isn't fungible litres pooled in a tank - each unit sold has a
    specific indent rate that IS its cost - so storing both rates here
    locks the exact per-line profit and dealer commission at the moment of
    sale, independent of wherever the rates move to afterwards.
    """

    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=False)
    shift_id = db.Column(db.Integer, db.ForeignKey("shift.id"), nullable=False)
    entry_date = db.Column(db.Date, nullable=False)
    quantity = db.Column(db.Float, nullable=False)
    retail_rate = db.Column(db.Float, nullable=False)
    purchase_rate = db.Column(db.Float, nullable=False)
    amount = db.Column(db.Float, nullable=False)
    method = db.Column(db.String(10), nullable=False, default="cash")  # cash | bank | credit
    bank_account_id = db.Column(db.Integer, db.ForeignKey("bank_account.id"), nullable=True)
    account_id = db.Column(db.Integer, db.ForeignKey("account.id"), nullable=True)  # set only when method == "credit"
    note = db.Column(db.String(200))
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    recorded_at = db.Column(db.DateTime, default=datetime.now)

    product = db.relationship("Product", backref="sales")
    shift = db.relationship("Shift")
    bank_account = db.relationship("BankAccount", backref="product_sales")
    account = db.relationship("Account", backref="product_sales")
    user = db.relationship("User")


class ProductPurchase(db.Model):
    """Stock received for a product. payment_type/method/bank_account_id/
    account_id follow StockPurchase's split exactly (see StockPurchase's
    docstring) - defined now for the same one-migration reason as
    ProductSale, not wired up yet.

    A NEGATIVE quantity records a return to the supplier or a stock-count
    correction - the real workbook does exactly this rather than keeping a
    separate "return" table - in which case total_cost must be negative to
    match: the sign of quantity and total_cost must always agree, or a
    return would look like stock arriving for free (or a purchase would
    look like a refund).
    """

    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=False)
    entry_date = db.Column(db.Date, nullable=False)
    quantity = db.Column(db.Float, nullable=False)
    unit_cost = db.Column(db.Float, nullable=False)
    total_cost = db.Column(db.Float, nullable=False)
    payment_type = db.Column(db.String(10), nullable=False, default="cash")  # cash | credit
    method = db.Column(db.String(10), nullable=False, default="cash")  # cash | bank (only when payment_type == cash)
    bank_account_id = db.Column(db.Integer, db.ForeignKey("bank_account.id"), nullable=True)
    account_id = db.Column(db.Integer, db.ForeignKey("account.id"), nullable=True)  # supplier, when payment_type == credit
    note = db.Column(db.String(200))
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    recorded_at = db.Column(db.DateTime, default=datetime.now)

    product = db.relationship("Product", backref="purchases")
    bank_account = db.relationship("BankAccount", backref="product_purchases")
    account = db.relationship("Account", backref="product_purchases")
    user = db.relationship("User")
