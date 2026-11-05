"""Pure selection contracts; no app imports, workers, NAS or live database."""
import sqlite3
import unittest

from services.voucher_expiry_policy import (
    QUOTA_REASONS, TIME_REASONS, category_sql, expiry_category,
)


class ExpirySelection(unittest.TestCase):
    def test_classification_matches_sql_for_supported_and_unknown_reasons(self):
        values = list(QUOTA_REASONS | TIME_REASONS) + [
            None, '', 'manual', 'manual quota note', '  QUOTA EXCEEDED  ',
            'تم إنهاء الاشتراك يدوياً بواسطة الإدارة',
        ]
        with sqlite3.connect(':memory:') as conn:
            for reason in values:
                actual = conn.execute(
                    'SELECT '+category_sql()+' FROM (SELECT ? expire_reason)',
                    (reason,),
                ).fetchone()[0]
                self.assertEqual(actual, expiry_category(reason), reason)

    def test_connection_time_quota_is_time_not_data(self):
        self.assertEqual(expiry_category('time quota exceeded'), 'time')
        self.assertEqual(expiry_category('تم استهلاك رصيد الوقت المسموح للباقة'), 'time')

    def test_unknown_and_manual_are_not_implicitly_time(self):
        for reason in (None, '', 'unknown', 'manual quota note'):
            self.assertEqual(expiry_category(reason), 'other')

    def test_column_identifier_is_validated(self):
        self.assertIn('v.expire_reason', category_sql('v.expire_reason'))
        for column in ('expire_reason); DELETE', 'v.*', 'v..expire_reason', ''):
            with self.assertRaises(ValueError):
                category_sql(column)


if __name__ == '__main__':
    unittest.main()
