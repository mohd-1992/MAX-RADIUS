"""Pure resource and report contracts; no app or database imports."""
import copy
from decimal import Decimal
import unittest
from services.db_memory_profile import plan
from services.sales_reporting_service import package_metrics
from services.network_traffic_service import sessions_sql


class Resources(unittest.TestCase):
    def test_db_profile_preserves_data_durability_and_innodb(self):
        before=dict(image='mariadb:10.11.8',command='--innodb-buffer-pool-size=512M --innodb-flush-log-at-trx-commit=2',
                    environment={'MYSQL_DATABASE':'fixture'},volumes=['data:/var/lib/mysql'],networks=['private'],ports=['3307:3306'])
        snapshot=copy.deepcopy(before);result=plan(before,'glibc')
        self.assertEqual(before,snapshot);self.assertEqual(result['before'],snapshot)
        for field in ('image','command','networks','ports'):self.assertEqual(result['after'][field],before[field])
        self.assertEqual(result['after']['environment']['MALLOC_ARENA_MAX'],'2');self.assertFalse(result['automatic_apply'])
        self.assertEqual(plan(result['after'],'glibc')['after'],result['after'])

    def test_profile_fails_closed_for_conflicts_or_partial_shared_scope(self):
        for service,allocator,kwargs in [({'image':'mariadb:10.11'},'musl',{}),({'image':'mysql:8'},'glibc',{}),
            ({'image':'mariadb:10.11','command':'--key-buffer-size=32M'},'glibc',{}),
            ({'image':'mariadb:10.11'},'glibc',{'shared':True,'participants':['a','b'],'approved_participants':['a']})]:
            with self.subTest(service=service),self.assertRaises(ValueError):plan(service,allocator,profile='conservative',**kwargs)

    def test_small_caches_are_explicit_and_config_matches_packaged_file(self):
        from pathlib import Path
        service={'image':'mariadb:10.11','volumes':['data:/var/lib/mysql']}
        self.assertEqual(plan(service,'glibc')['after']['volumes'],service['volumes'])
        result=plan(service,'glibc',profile='conservative')
        self.assertEqual(result['managed_config'],Path('deploy/db-memory/conservative.cnf').read_text())
        self.assertEqual(plan(result['after'],'glibc',profile='conservative')['after'],result['after'])

    def test_package_partition_metrics_preserve_decimal_and_unknown_cost(self):
        rows=[dict(sold_count=2,total_gross_revenue=Decimal('100.11'),total_isp_revenue=Decimal('80.01'),unknown_cost_count=1),
              dict(sold_count=1,total_gross_revenue=Decimal('3.33'),total_isp_revenue=Decimal('2.22'),unknown_cost_count=0)]
        out=package_metrics(rows)
        self.assertEqual(out['total_gross_revenue'],Decimal('103.44'));self.assertEqual(out['total_isp_revenue'],Decimal('82.23'))
        self.assertEqual(out['sold_count'],3);self.assertEqual(out['unknown_cost_count'],1)
        self.assertIsNone(package_metrics([])['unknown_cost_count'])
        self.assertEqual(str(package_metrics([dict(total_reseller_profit=Decimal('0.00'))])['total_reseller_profit']),'0.00')
        self.assertEqual(str(package_metrics([])['total_reseller_profit']),'0.00')

    def test_nas_filter_is_parameterized_in_all_four_sources(self):
        sql,params=sessions_sql(nas='192.0.2.1')
        self.assertEqual(params,('192.0.2.1',)*4)
        self.assertEqual(sql.count("COALESCE(nasipaddress,'')=%s"),4)
        self.assertNotIn('192.0.2.1',sql)
