"""A deterministic dataset covering every entry kind this app records.

Deliberately literal: no randomness, no date.today(), no auto-increment
assumptions. The same call always produces byte-identical data, which is
what lets golden_master.py detect a behaviour change rather than noise.

Every row passes pump_id explicitly. This runs with no request context,
so tenancy.py's before_flush auto-stamp has no pump to resolve and would
(correctly) refuse to guess one - see its RuntimeError.
"""
from datetime import date, datetime, timedelta

from extensions import db
from models import (
    Account, BankAccount, BankSale, CashAccount, CashDeposit, CashHandover,
    CreditGiven, DirectSale, Dispenser, EmployeeLoan, Expense,
    FuelPriceHistory, FuelType, Nozzle, NozzleTesting, OtherIncome, Product,
    ProductPurchase, ProductRateHistory, ProductSale, Pump, Receipt,
    SalaryPayment, Sale, SalesReturn, Shift, StockPurchase, SupplierPayment,
    Tank, TankDip, TankerDeal, User,
)

# Fixed anchor. Every date below is an offset from this, so the snapshot
# does not shift when the calendar does.
D0 = date(2026, 6, 1)


def d(n):
    return date.fromordinal(D0.toordinal() + n)


class _Stamper:
    """Every entry model defaults recorded_at to datetime.now, which makes
    two runs of identical code differ - and the ledger feed SORTS on it, so
    rows also render in a different order. Both showed up in the
    golden-master determinism gate. Hand out a fixed, strictly increasing
    timestamp instead: distinct so ordering is total (no tie for a stable
    sort to resolve differently), fixed so it never moves."""

    def __init__(self):
        self.n = 0

    def __call__(self, *objs):
        for o in objs:
            if hasattr(type(o), "recorded_at"):
                self.n += 1
                o.recorded_at = datetime(2026, 6, 1, 0, 0, 0) + timedelta(seconds=self.n)
            db.session.add(o)
        return objs[0] if len(objs) == 1 else objs


def seed(pump_name="Golden Pump", password="goldenmasterpw1"):
    """Build one fully-populated pump. Returns a dict of handy ids."""
    add = _Stamper()

    pump = Pump(name=pump_name, created_at=datetime(2026, 5, 1, 9, 0, 0))
    db.session.add(pump)
    db.session.flush()
    P = pump.id

    owner = User(pump_id=P, username="owner", email=f"owner-{P}@golden.test",
                 role="owner", created_at=datetime(2026, 5, 1, 9, 0, 0))
    owner.set_password(password)
    staff = User(pump_id=P, username="staff", email=f"staff-{P}@golden.test",
                 role="staff", created_at=datetime(2026, 5, 1, 9, 0, 0))
    staff.set_password(password)
    db.session.add_all([owner, staff])
    db.session.flush()
    U = owner.id

    # --- structure -------------------------------------------------------
    shift_a = Shift(pump_id=P, name="Morning", sort_order=0)
    shift_b = Shift(pump_id=P, name="Evening", sort_order=1)
    db.session.add_all([shift_a, shift_b])

    petrol = FuelType(pump_id=P, name="Petrol", price_per_liter=272.5,
                      entry_mode="meter", direct_entry_combined=False)
    diesel = FuelType(pump_id=P, name="Diesel", price_per_liter=281.0,
                      entry_mode="meter", direct_entry_combined=False)
    db.session.add_all([petrol, diesel])
    db.session.flush()

    # Price history, so price_on_date() has something to walk.
    add(*[
        FuelPriceHistory(pump_id=P, fuel_type_id=petrol.id, price_per_liter=268.0, effective_date=d(0)),
        FuelPriceHistory(pump_id=P, fuel_type_id=petrol.id, price_per_liter=272.5, effective_date=d(4)),
        FuelPriceHistory(pump_id=P, fuel_type_id=diesel.id, price_per_liter=277.0, effective_date=d(0)),
        FuelPriceHistory(pump_id=P, fuel_type_id=diesel.id, price_per_liter=281.0, effective_date=d(5)),
    ])

    tank_p = Tank(pump_id=P, number=1, fuel_type_id=petrol.id, capacity_liters=20000,
                  starting_stock_liters=8000, starting_stock_date=d(0),
                  starting_stock_cost_per_liter=260.0, low_stock_threshold=2000)
    tank_d = Tank(pump_id=P, number=2, fuel_type_id=diesel.id, capacity_liters=25000,
                  starting_stock_liters=11000, starting_stock_date=d(0),
                  starting_stock_cost_per_liter=266.0, low_stock_threshold=2500)
    db.session.add_all([tank_p, tank_d])

    disp = Dispenser(pump_id=P, number=1)
    db.session.add(disp)
    db.session.flush()

    noz_p = Nozzle(pump_id=P, dispenser_id=disp.id, nozzle_number=1, tank_id=tank_p.id)
    noz_d = Nozzle(pump_id=P, dispenser_id=disp.id, nozzle_number=2, tank_id=tank_d.id)
    db.session.add_all([noz_p, noz_d])

    cash = CashAccount(pump_id=P, opening_balance=150000.0, opening_balance_date=d(0),
                       created_at=datetime(2026, 5, 1, 9, 0, 0))
    bank = BankAccount(pump_id=P, name="Meezan Current", opening_balance=900000.0,
                       opening_balance_date=d(0), created_at=datetime(2026, 5, 1, 9, 0, 0))
    db.session.add_all([cash, bank])

    # --- accounts, including a parent with two children ------------------
    parent = Account(pump_id=P, name="LAWI Group", account_type="customer",
                     opening_balance=0, opening_balance_date=d(0))
    db.session.add(parent)
    db.session.flush()
    child_a = Account(pump_id=P, name="LAWI - JV", account_type="customer",
                      opening_balance=25000.0, opening_balance_date=d(0),
                      parent_account_id=parent.id)
    child_b = Account(pump_id=P, name="LAWI - Sarwar", account_type="customer",
                      opening_balance=12000.0, opening_balance_date=d(0),
                      parent_account_id=parent.id)
    plain = Account(pump_id=P, name="Walk-in Khan", account_type="customer",
                    opening_balance=5000.0, opening_balance_date=d(0), notes="cash regular")
    supplier = Account(pump_id=P, name="PSO Depot", account_type="supplier",
                       opening_balance=-40000.0, opening_balance_date=d(0))
    employee = Account(pump_id=P, name="Attendant Bilal", account_type="employee",
                       opening_balance=0, opening_balance_date=d(0))
    db.session.add_all([child_a, child_b, plain, supplier, employee])
    db.session.flush()

    # --- products --------------------------------------------------------
    oil = Product(pump_id=P, name="Engine Oil 4L", category="lubricant", pack_size="4L",
                  unit="piece", purchase_rate=2600.0, retail_rate=3200.0,
                  opening_stock=40, opening_stock_date=d(0), low_stock_threshold=5)
    db.session.add(oil)
    db.session.flush()
    add(*[
        ProductRateHistory(pump_id=P, product_id=oil.id, purchase_rate=2600.0,
                           retail_rate=3200.0, effective_date=d(0)),
        ProductRateHistory(pump_id=P, product_id=oil.id, purchase_rate=2600.0,
                           retail_rate=3350.0, effective_date=d(5)),
    ])

    db.session.flush()
    S1, S2 = shift_a.id, shift_b.id
    BK = bank.id

    # --- nozzle readings, across two shifts and several days -------------
    prev_p, prev_d = 100000.0, 200000.0
    for i, day in enumerate((0, 1, 2, 5, 6)):
        for shift, step_p, step_d in ((S1, 420.0, 610.0), (S2, 380.0, 545.0)):
            add(Sale(pump_id=P, user_id=U, nozzle_id=noz_p.id, shift_id=shift,
                     entry_date=d(day), previous_reading=prev_p,
                     current_reading=prev_p + step_p, liters=step_p, testing_liters=0,
                     price_per_liter=268.0 if day < 4 else 272.5,
                     total_amount=round(step_p * (268.0 if day < 4 else 272.5), 2)))
            prev_p += step_p
            add(Sale(pump_id=P, user_id=U, nozzle_id=noz_d.id, shift_id=shift,
                     entry_date=d(day), previous_reading=prev_d,
                     current_reading=prev_d + step_d, liters=step_d, testing_liters=0,
                     price_per_liter=277.0 if day < 5 else 281.0,
                     total_amount=round(step_d * (277.0 if day < 5 else 281.0), 2)))
            prev_d += step_d

    add(NozzleTesting(pump_id=P, user_id=U, nozzle_id=noz_p.id, shift_id=S1,
                      entry_date=d(1), liters=5.0, note="daily test"))
    add(DirectSale(pump_id=P, user_id=U, tank_id=tank_d.id, shift_id=S1,
                   entry_date=d(2), liters=300.0, price_per_liter=277.0,
                   total_amount=83100.0))

    # --- credit / receipts ----------------------------------------------
    add(CreditGiven(pump_id=P, user_id=U, account_id=child_a.id, fuel_type_id=diesel.id,
                    shift_id=S1, entry_date=d(1), liters=200.0, price_per_liter=277.0,
                    amount=55400.0, vehicle_number="LEA-1234"))
    add(CreditGiven(pump_id=P, user_id=U, account_id=child_b.id, fuel_type_id=petrol.id,
                    shift_id=S2, entry_date=d(2), liters=150.0, price_per_liter=268.0,
                    amount=40200.0, vehicle_number="LEB-9999"))
    add(CreditGiven(pump_id=P, user_id=U, account_id=plain.id, fuel_type_id=petrol.id,
                    shift_id=S1, entry_date=d(5), liters=80.0, price_per_liter=270.0,
                    amount=21600.0, note="discounted rate"))
    add(Receipt(pump_id=P, user_id=U, account_id=child_a.id, entry_date=d(3),
                amount=30000.0, method="cash"))
    add(Receipt(pump_id=P, user_id=U, account_id=child_b.id, entry_date=d(6),
                amount=15000.0, method="bank", bank_account_id=BK))

    # --- purchases / supplier payments ----------------------------------
    add(StockPurchase(pump_id=P, user_id=U, tank_id=tank_p.id, entry_date=d(1),
                      liters=6000.0, cost=1560000.0, payment_type="credit",
                      account_id=supplier.id))
    add(StockPurchase(pump_id=P, user_id=U, tank_id=tank_d.id, entry_date=d(4),
                      liters=5000.0, cost=1330000.0, payment_type="cash", method="bank",
                      bank_account_id=BK))
    add(SupplierPayment(pump_id=P, user_id=U, account_id=supplier.id, entry_date=d(5),
                        amount=500000.0, method="bank", bank_account_id=BK))

    # --- money out -------------------------------------------------------
    add(Expense(pump_id=P, user_id=U, entry_date=d(1), category="Electricity",
                description="June bill", amount=48000.0, method="cash"))
    add(Expense(pump_id=P, user_id=U, entry_date=d(4), category="Repairs",
                description="nozzle seal", amount=7500.0, method="bank",
                bank_account_id=BK))
    add(SalaryPayment(pump_id=P, user_id=U, account_id=employee.id, entry_date=d(6),
                      period_label="June", gross_amount=45000.0, deduction_amount=5000.0,
                      method="cash"))
    add(EmployeeLoan(pump_id=P, user_id=U, account_id=employee.id, entry_date=d(2),
                     amount=10000.0, kind="loan", method="cash"))
    add(EmployeeLoan(pump_id=P, user_id=U, account_id=employee.id, entry_date=d(5),
                     amount=3000.0, kind="drawing", method="cash"))

    # --- banking ---------------------------------------------------------
    add(BankSale(pump_id=P, user_id=U, bank_account_id=BK, shift_id=S1,
                 entry_date=d(2), amount=125000.0, note="card machine"))
    add(CashDeposit(pump_id=P, user_id=U, bank_account_id=BK, entry_date=d(3),
                    amount=200000.0, note="daily deposit"))
    add(CashHandover(pump_id=P, user_id=U, entry_date=d(1), shift_id=S1,
                     attendant_id=employee.id, declared_amount=95000.0))

    # --- stock movement --------------------------------------------------
    add(TankDip(pump_id=P, user_id=U, tank_id=tank_p.id, entry_date=d(2),
                dip_cm=110.0, dip_liters=7400.0, water_cm=1.0))
    add(TankDip(pump_id=P, user_id=U, tank_id=tank_d.id, entry_date=d(5),
                dip_cm=140.0, dip_liters=12100.0, water_cm=0.0))
    add(SalesReturn(pump_id=P, user_id=U, entry_date=d(3), shift_id=S1,
                    fuel_type_id=petrol.id, tank_id=tank_p.id, liters=20.0,
                    price_per_liter=268.0, amount=5360.0, method="cash"))

    # --- non-fuel --------------------------------------------------------
    add(ProductPurchase(pump_id=P, user_id=U, product_id=oil.id, entry_date=d(1),
                        quantity=20, unit_cost=2600.0, total_cost=52000.0,
                        payment_type="credit", account_id=supplier.id))
    add(ProductSale(pump_id=P, user_id=U, product_id=oil.id, shift_id=S1,
                    entry_date=d(2), quantity=6, retail_rate=3200.0,
                    purchase_rate=2600.0, amount=19200.0, method="cash"))
    add(ProductSale(pump_id=P, user_id=U, product_id=oil.id, shift_id=S2,
                    entry_date=d(6), quantity=3, retail_rate=3350.0,
                    purchase_rate=2600.0, amount=10050.0, method="credit",
                    account_id=plain.id))
    add(OtherIncome(pump_id=P, user_id=U, entry_date=d(4), description="Air pump coins",
                    amount=2200.0, method="cash"))

    # --- tanker pass-through deals, all three payment shapes -------------
    add(TankerDeal(pump_id=P, user_id=U, entry_date=d(3), fuel_type_id=diesel.id,
                   liters=10000.0, purchase_cost=2700000.0,
                   purchase_payment_type="credit", supplier_account_id=supplier.id,
                   sale_amount=2810000.0, sale_payment_type="credit",
                   customer_account_id=child_a.id, note="pass-through"))
    add(TankerDeal(pump_id=P, user_id=U, entry_date=d(6), fuel_type_id=petrol.id,
                   liters=4000.0, purchase_cost=1060000.0,
                   purchase_payment_type="bank", purchase_bank_account_id=BK,
                   sale_amount=1104000.0, sale_payment_type="cash"))

    db.session.commit()
    return {
        "pump_id": P, "owner_id": U, "owner_email": owner.email,
        "password": password,
        "shift_ids": [S1, S2],
        "fuel_ids": {"petrol": petrol.id, "diesel": diesel.id},
        "tank_ids": {"petrol": tank_p.id, "diesel": tank_d.id},
        "nozzle_ids": {"petrol": noz_p.id, "diesel": noz_d.id},
        "account_ids": {"parent": parent.id, "child_a": child_a.id,
                        "child_b": child_b.id, "plain": plain.id,
                        "supplier": supplier.id, "employee": employee.id},
        "bank_id": BK, "product_id": oil.id,
        "dates": [d(i).isoformat() for i in range(8)],
    }
