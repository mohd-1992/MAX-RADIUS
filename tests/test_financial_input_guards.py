"""Boundary checks without importing application workers; native SQL tests cover commit semantics."""
import ast
from decimal import Decimal, InvalidOperation
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


def function(path, name):
    tree = ast.parse((ROOT / path).read_text(encoding='utf-8'))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    scope = dict(Decimal=Decimal, InvalidOperation=InvalidOperation)
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), scope)
    return scope[name]


class FinancialInputTests(unittest.TestCase):
    def test_legacy_deposit_refuses_invalid_or_nonpositive_money(self):
        parse = function('services/reseller_service.py', '_deposit_amount')
        for value in ('NaN', 'Infinity', '-Infinity', '-1', '0', '0.001', 'bad', None, '100000000'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse(value)
        self.assertEqual(parse('10.01'), Decimal('10.01'))

    def test_catalog_rejects_invalid_cost_value_and_type(self):
        terms = function('services/loyalty_rewards_service.py', '_reward_terms')
        for args in ((0, 'custom', 0), (-1, 'balance_credit', 1), ('NaN', 'custom', 0),
                     (1.5, 'custom', 0), (10, 'balance_credit', -1),
                     (10, 'balance_credit', 'Infinity'), (10, 'balance_credit', '1.5'),
                     (10, 'data_bonus_mb', 0), (10, 'validity_days', 1.5), (10, 'unknown', 1)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                terms(*args)

    def test_existing_integer_catalog_and_custom_legacy_label_remain_valid(self):
        terms = function('services/loyalty_rewards_service.py', '_reward_terms')
        for kind, value in (('balance_credit', 5), ('data_bonus_mb', 1024),
                            ('validity_days', 3), ('custom', 0), ('discount_voucher', 0)):
            with self.subTest(kind=kind):
                self.assertEqual(terms(10, kind, value), (10, Decimal(value)))


if __name__ == '__main__':
    unittest.main()
