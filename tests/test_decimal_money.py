from decimal import Decimal
import unittest

from sqlalchemy import Numeric


from app.models import Product, Sale
from app.money import MONEY_MAX, money_decimal, money_json, money_sum


class DecimalMoneyTests(unittest.TestCase):
    def test_rounds_money_to_cents_with_half_up_policy(self):
        self.assertEqual(money_decimal("0.005"), Decimal("0.01"))
        self.assertEqual(money_decimal("12.004"), Decimal("12.00"))
        self.assertEqual(money_decimal("-1.235", nonnegative=False), Decimal("-1.24"))

    def test_rejects_invalid_nonfinite_negative_and_out_of_range_money(self):
        for value in ("NaN", "Infinity", "-Infinity", "not-money"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    money_decimal(value)
        with self.assertRaises(ValueError):
            money_decimal("-0.01")
        with self.assertRaises(ValueError):
            money_decimal(MONEY_MAX + Decimal("0.01"))

    def test_sums_and_serializes_without_binary_float_arithmetic(self):
        total = money_sum(("0.10", "0.10", "0.10"))
        self.assertEqual(total, Decimal("0.30"))
        self.assertEqual(money_json(total), "0.30")

    def test_all_persisted_pos_money_uses_fixed_precision_numeric(self):
        expected = {
            Product.__table__.c.cost_price,
            Product.__table__.c.sale_price,
            Sale.__table__.c.unit_price,
            Sale.__table__.c.total,
            Sale.__table__.c.unit_cost,
        }
        for column in expected:
            with self.subTest(column=str(column)):
                self.assertIsInstance(column.type, Numeric)
                self.assertEqual(column.type.precision, 14)
                self.assertEqual(column.type.scale, 2)


if __name__ == "__main__":
    unittest.main()
