from datetime import datetime

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from extensions import db


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False)  # "owner" or "staff"

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def is_owner(self):
        return self.role == "owner"


class FuelType(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)
    price_per_liter = db.Column(db.Float, nullable=False, default=0)


class Tank(db.Model):
    """A physical storage tank. Several tanks can share a fuel type.

    Stock is never stored as a mutable counter here - it's always
    calculated from starting_stock_liters + purchases - sales, filtered to
    a date, so backfilling or editing a past ledger entry can never leave
    stock out of sync (see book_stock() in app.py).
    """

    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.Integer, nullable=False, unique=True)
    fuel_type_id = db.Column(db.Integer, db.ForeignKey("fuel_type.id"), nullable=False)
    capacity_liters = db.Column(db.Float, nullable=False)
    starting_stock_liters = db.Column(db.Float, nullable=False, default=0)
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
        return round(
            self.opening_balance
            + credit_given_total
            - receipts_total
            - purchases_credit_total
            + supplier_payments_total
            + loans_total,
            2,
        )


class Sale(db.Model):
    """A debit-side ledger entry: one nozzle's meter reading on one date.

    At most one Sale exists per (nozzle_id, entry_date) - re-submitting a
    reading for a date that already has one updates it in place rather
    than creating a duplicate, which is what makes backfilling/editing a
    past date safe.
    """

    id = db.Column(db.Integer, primary_key=True)
    nozzle_id = db.Column(db.Integer, db.ForeignKey("nozzle.id"), nullable=False)
    entry_date = db.Column(db.Date, nullable=False)
    previous_reading = db.Column(db.Float, nullable=False)
    current_reading = db.Column(db.Float, nullable=False)
    liters = db.Column(db.Float, nullable=False)
    price_per_liter = db.Column(db.Float, nullable=False)
    total_amount = db.Column(db.Float, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    recorded_at = db.Column(db.DateTime, default=datetime.now)

    nozzle = db.relationship("Nozzle", backref="sales")
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
    entry_date = db.Column(db.Date, nullable=False)
    liters = db.Column(db.Float, nullable=False)
    price_per_liter = db.Column(db.Float, nullable=False)
    amount = db.Column(db.Float, nullable=False)
    note = db.Column(db.String(200))
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    recorded_at = db.Column(db.DateTime, default=datetime.now)

    account = db.relationship("Account", backref="credit_entries")
    fuel_type = db.relationship("FuelType")
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
        return round(
            self.opening_balance
            + sales_total
            + deposits_total
            + receipts_total
            - loans_total
            - expenses_total
            - fuel_purchases_total
            - supplier_payments_total,
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
    entry_date = db.Column(db.Date, nullable=False)
    amount = db.Column(db.Float, nullable=False)
    note = db.Column(db.String(200))
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    recorded_at = db.Column(db.DateTime, default=datetime.now)

    bank_account = db.relationship("BankAccount", backref="bank_sales")
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
    """

    id = db.Column(db.Integer, primary_key=True)
    tank_id = db.Column(db.Integer, db.ForeignKey("tank.id"), nullable=False)
    entry_date = db.Column(db.Date, nullable=False)
    dip_liters = db.Column(db.Float, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    recorded_at = db.Column(db.DateTime, default=datetime.now)

    tank = db.relationship("Tank", backref="dips")
    user = db.relationship("User")
