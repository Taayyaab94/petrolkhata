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


class Customer(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    phone = db.Column(db.String(30))
    opening_balance = db.Column(db.Float, nullable=False, default=0)
    opening_balance_date = db.Column(db.Date, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.now)

    @property
    def balance(self):
        """Positive balance = customer owes the pump money."""
        credit_total = sum(c.amount for c in self.credit_entries)
        payments_total = sum(p.amount for p in self.payments)
        return round(self.opening_balance + credit_total - payments_total, 2)


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
    customer_id = db.Column(db.Integer, db.ForeignKey("customer.id"), nullable=False)
    fuel_type_id = db.Column(db.Integer, db.ForeignKey("fuel_type.id"), nullable=False)
    entry_date = db.Column(db.Date, nullable=False)
    liters = db.Column(db.Float, nullable=False)
    price_per_liter = db.Column(db.Float, nullable=False)
    amount = db.Column(db.Float, nullable=False)
    note = db.Column(db.String(200))
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    recorded_at = db.Column(db.DateTime, default=datetime.now)

    customer = db.relationship("Customer", backref="credit_entries")
    fuel_type = db.relationship("FuelType")
    user = db.relationship("User")


class Supplier(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    phone = db.Column(db.String(30))
    opening_balance = db.Column(db.Float, nullable=False, default=0)
    opening_balance_date = db.Column(db.Date, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.now)

    @property
    def balance(self):
        """Positive balance = we owe the supplier money."""
        owed_total = sum(
            (p.cost or 0) for p in self.purchases if p.payment_type == "credit"
        )
        paid_total = sum(p.amount for p in self.payments)
        return round(self.opening_balance + owed_total - paid_total, 2)


class StockPurchase(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    tank_id = db.Column(db.Integer, db.ForeignKey("tank.id"), nullable=False)
    entry_date = db.Column(db.Date, nullable=False)
    liters = db.Column(db.Float, nullable=False)
    cost = db.Column(db.Float)
    payment_type = db.Column(db.String(10), nullable=False, default="cash")  # cash | credit
    supplier_id = db.Column(db.Integer, db.ForeignKey("supplier.id"), nullable=True)
    note = db.Column(db.String(200))
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    recorded_at = db.Column(db.DateTime, default=datetime.now)

    tank = db.relationship("Tank", backref="purchases")
    supplier = db.relationship("Supplier", backref="purchases")
    user = db.relationship("User")


class SupplierPayment(db.Model):
    """A credit-side ledger entry: money we paid a supplier against fuel
    we previously took on credit. Mirrors CustomerPayment - reduces what
    we owe the supplier the same way a customer receipt reduces what a
    customer owes us."""

    id = db.Column(db.Integer, primary_key=True)
    supplier_id = db.Column(db.Integer, db.ForeignKey("supplier.id"), nullable=False)
    entry_date = db.Column(db.Date, nullable=False)
    amount = db.Column(db.Float, nullable=False)
    note = db.Column(db.String(200))
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    recorded_at = db.Column(db.DateTime, default=datetime.now)

    supplier = db.relationship("Supplier", backref="payments")
    user = db.relationship("User")


class CustomerPayment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customer.id"), nullable=False)
    entry_date = db.Column(db.Date, nullable=False)
    amount = db.Column(db.Float, nullable=False)
    note = db.Column(db.String(200))
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    recorded_at = db.Column(db.DateTime, default=datetime.now)

    customer = db.relationship("Customer", backref="payments")
    user = db.relationship("User")


class Employee(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    phone = db.Column(db.String(30))
    opening_balance = db.Column(db.Float, nullable=False, default=0)
    opening_balance_date = db.Column(db.Date, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.now)

    @property
    def balance(self):
        """Positive balance = employee owes the pump money (loans/advances)."""
        loans_total = sum(l.amount for l in self.loans)
        repayments_total = sum(r.amount for r in self.repayments)
        return round(self.opening_balance + loans_total - repayments_total, 2)


class EmployeeLoan(db.Model):
    """A credit-side ledger entry: a loan or advance given to an employee,
    the same way CreditGiven works for customers - it increases what the
    employee owes the pump."""

    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"), nullable=False)
    entry_date = db.Column(db.Date, nullable=False)
    amount = db.Column(db.Float, nullable=False)
    note = db.Column(db.String(200))
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    recorded_at = db.Column(db.DateTime, default=datetime.now)

    employee = db.relationship("Employee", backref="loans")
    user = db.relationship("User")


class EmployeeRepayment(db.Model):
    """A debit-side ledger entry: an employee paying back a loan/advance.
    Mirrors CustomerPayment."""

    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"), nullable=False)
    entry_date = db.Column(db.Date, nullable=False)
    amount = db.Column(db.Float, nullable=False)
    note = db.Column(db.String(200))
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    recorded_at = db.Column(db.DateTime, default=datetime.now)

    employee = db.relationship("Employee", backref="repayments")
    user = db.relationship("User")


class Expense(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    entry_date = db.Column(db.Date, nullable=False)
    category = db.Column(db.String(80), nullable=False)
    description = db.Column(db.String(200))
    amount = db.Column(db.Float, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    recorded_at = db.Column(db.DateTime, default=datetime.now)

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
        return round(self.opening_balance + sales_total + deposits_total, 2)


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
