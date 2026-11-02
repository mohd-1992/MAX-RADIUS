"""Read-only package grouping checks against the existing synthetic fixture."""
import os
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from jinja2 import Environment

assert sys.argv[1:] == ['--run'] and os.environ.get('DB_NAME') == 'sales266_fixture'
threading.Thread.start = lambda self: None

import services.sales_reporting_service as sales
from database.db import query_all


class PackageGrouping(unittest.TestCase):
    def setUp(self):
        self.owner = patch('core.ownership.owner_id', return_value=None)
        self.owner.start()
        self.addCleanup(self.owner.stop)

    def report(self, **args):
        args.setdefault('period', 'all')
        return sales.report(args, timezone='Asia/Aden')

    def test_one_row_per_package_matches_original_ledger(self):
        expected = query_all('SELECT package_name AS name,COUNT(*) AS n,SUM(price) AS gross '
                             'FROM wisp_voucher_sales GROUP BY package_name')
        rows = self.report()['package_breakdown']
        self.assertEqual(len(rows), len({r['name'] for r in rows}))
        self.assertEqual({r['name']:(r['sold_count'], r['total_gross_revenue']) for r in rows},
                         {r['name']:(r['n'], r['gross']) for r in expected})

    def test_current_nas_names_and_today_default(self):
        catalog = {}
        for row in query_all("SELECT ip_address AS address,name AS label FROM wisp_nas_devices ORDER BY id") + query_all("SELECT nasname AS address,shortname AS label FROM nas ORDER BY id"):
            if row['label'] and row['label'].strip():
                catalog.setdefault(row['address'], row['label'].strip())
        result = self.report(server_sort='nas', server_dir='asc')
        checked = 0
        for row in result['recent_sales'] + result['server_breakdown']:
            if row['nas_address'] in catalog:
                self.assertEqual(row['nas_label'], catalog[row['nas_address']])
                checked += 1
        # Virtual catalogs exercise stale-IP snapshots without writing NAS rows.
        expression = sales.NAS_LABEL.replace('FROM wisp_nas_devices n',
            "FROM (SELECT 1 AS id,'192.0.2.10' AS ip_address,%s AS name) n")
        expression = expression.replace('FROM nas n',
            "FROM (SELECT 1 AS id,'192.0.2.10' AS nasname,%s AS shortname) n")
        for configured, shortname, expected in [('Router name','Legacy name','Router name'),
                ('  ','Legacy name','Legacy name'), ('', '', '192.0.2.10')]:
            row = query_all('SELECT '+expression+" AS label FROM (SELECT '192.0.2.10' AS nas_address,'192.0.2.10' AS nas_label) x",
                            (configured,shortname))[0]
            self.assertEqual(row['label'], expected)
        default = sales.report({}, timezone='Asia/Aden')
        explicit = self.report(period='today')
        self.assertEqual(default['period'], 'today')
        self.assertEqual(default['total_items_count'], explicit['total_items_count'])
        self.assertEqual(default['gross_sales'], explicit['gross_sales'])
        self.assertEqual(default['all_packages_list'], result['all_packages_list'])

    def test_mixed_services_prices_and_filtered_partitions(self):
        # Process-local virtual ledger; no fixture rows are written or reseeded.
        virtual = sales.FROM.replace('s.package_name', "'Mixed synthetic package' AS package_name")
        virtual = virtual.replace('s.price',
            '(CASE WHEN MOD(s.id,2)=0 THEN 200.00 ELSE 100.00 END) AS price')
        with patch.object(sales, 'FROM', virtual):
            whole = self.report()
            self.assertEqual(len(whole['package_breakdown']), 1)
            row = whole['package_breakdown'][0]
            self.assertEqual(row['service_types'], ['hotspot','pppoe'])
            self.assertEqual(row['sold_count'], whole['total_items_count'])
            self.assertEqual(row['price'], '100.00 – 200.00')
            self.assertEqual(row['isp_unit_price'], '100.00 – 200.00')
            source = (Path(sales.__file__).parents[1] / 'web/templates/sales_reports.html').read_text()
            start = source.index('{% macro table_money')
            end = source.index('{%- endmacro %}', start) + len('{%- endmacro %}')
            rows_start = source.index('{% for p in package_breakdown %}')
            fragment = source[start:end] + source[rows_start:source.index('</tbody>', rows_start)]
            html = Environment(autoescape=True).from_string(fragment).render(
                package_breakdown=whole['package_breakdown'], currency_symbol='CUR')
            self.assertEqual(html.count('<tr '), 1)
            self.assertIn('Hotspot', html)
            self.assertIn('PPPoE', html)
            self.assertIn('100.00 – 200.00', html)
            parts = [self.report(service_type=kind) for kind in ('hotspot','pppoe','unknown')]
            for field in ('gross_sales','net_isp_revenue','total_reseller_commissions','total_items_count'):
                self.assertEqual(sum(part[field] for part in parts), whole[field])
            self.assertTrue(all(len(part['package_breakdown']) <= 1 for part in parts))

    def test_all_package_sorts_preserve_grouping_and_money(self):
        baseline = self.report()
        for key in sales.SORT_COLUMNS['package']:
            for direction in ('asc','desc'):
                data = self.report(package_sort=key, package_dir=direction)
                self.assertEqual(data['gross_sales'], baseline['gross_sales'])
                self.assertEqual(len(data['package_breakdown']), len(baseline['package_breakdown']))

    def test_reseller_scope_is_preserved(self):
        with patch('core.ownership.owner_id', return_value=999999):
            self.assertEqual(self.report()['package_breakdown'], [])


if __name__ == '__main__':
    unittest.main(argv=[sys.argv[0]])
