"""Isolated contracts; no application factory, Docker, database or NAS."""
import datetime as dt
import unittest
from services.sales_reporting_service import date_bounds, filters, export_value, month_label, traffic_for_period
from services.sales_source_service import source_from_rows, server_key


class SalesContracts(unittest.TestCase):
    def test_default_period_is_today_but_all_remains_explicit(self):
        now = dt.datetime(2026, 10, 24, 22, tzinfo=dt.timezone.utc)
        selected, sql, params = filters({}, now=now)
        self.assertEqual(selected['period'], 'today')
        self.assertIn(dt.datetime(2026, 10, 25), params)
        self.assertIn(dt.datetime(2026, 10, 26), params)
        selected, sql, params = filters({'period':'all'}, now=now)
        self.assertEqual(selected['period'], 'all')
        self.assertNotIn('e.event_at>=', sql)

    def test_arabic_month_labels_keep_year(self):
        self.assertEqual(month_label('2026-10'), 'أكتوبر 2026')
        self.assertEqual(month_label('2025-10'), 'أكتوبر 2025')
        self.assertEqual(month_label(None), 'تاريخ غير محدد')

    def test_traffic_uses_sample_differences_and_owner_scope(self):
        calls=[]
        def query(sql, params=()):
            calls.append((sql,params))
            return [dict(epoch='x',marker='x')] if 's.epoch' in sql else []
        selected=dict(period='custom',date_from='2026-10-25',date_to='2026-10-25')
        data=traffic_for_period(query,selected,owner=7)
        self.assertIn(dt.datetime(2026,10,26),calls[-1][1])
        self.assertIn('wisp_traffic_rollups',calls[-1][0])
        self.assertNotIn('radacct_archive',calls[-1][0])
        self.assertEqual(calls[-1][1][-1],7)
        self.assertEqual(len(data['labels']),24)
        self.assertTrue(all(v is None for v in data['download']))

    def test_local_midnight_and_exclusive_end(self):
        now = dt.datetime(2026, 10, 24, 22, tzinfo=dt.timezone.utc)
        self.assertEqual(date_bounds('today', now=now), (dt.datetime(2026,10,25),dt.datetime(2026,10,26)))
        self.assertEqual(date_bounds('yesterday', now=now), (dt.datetime(2026,10,24),dt.datetime(2026,10,25)))

    def test_leap_and_open_custom_periods(self):
        self.assertEqual(date_bounds('custom','2024-02-29','2024-02-29'), (dt.datetime(2024,2,29),dt.datetime(2024,3,1)))
        self.assertEqual(date_bounds('custom','','2024-02-29')[0],None)
        for args in [('custom','2026-02-30',''),('custom','2026-10-26','2026-10-25'),('bad','','')]:
            with self.assertRaises(ValueError):date_bounds(*args)

    def test_filter_scope_and_bound_parameters(self):
        payload="x%' OR 1=1 --"
        _,sql,params=filters({'q':payload,'record_kind':'invoice'},owner=7)
        self.assertNotIn(payload,sql)
        self.assertIn("e.event_kind='voucher'",sql)
        self.assertEqual(params[:2],('invoice',7))
        self.assertIn('\\%',params[-1])

    def test_invalid_filters(self):
        for args in ({'record_kind':'all'},{'server':'1 OR 1=1'},{'reseller_id':'-1'},{'service_type':'invalid'},{'q':'x'*201}):
            with self.assertRaises(ValueError):filters(args)

    def test_saved_server_label_does_not_depend_on_optional_protocol_or_key(self):
        from services.sales_reporting_service import _label
        self.assertEqual(_label(dict(server_key='unknown',server_name='Recorded-HS',service_type='')),'Recorded-HS')
        self.assertEqual(_label(dict(server_name='',service_type='pppoe')),'غير محدد')
        self.assertEqual(_label(dict(server_name='P',service_type='pppoe',server_interface='bridge1')),'PPPoE / P / bridge1')

    def test_export_formula_injection(self):
        for text in ('=1+1',' +SUM(A1)','-cmd','@SUM(A1)'):
            self.assertTrue(export_value(text).startswith("'"))
        self.assertEqual(export_value('اسم عربي'),'اسم عربي')
        self.assertEqual(export_value(-5),-5)

    def row(self,server='HS1',port='ether1',nas='192.0.2.10'):
        return dict(nasipaddress=nas,calledstationid=server,nasportid=port,acctuniqueid='fixture',acctterminatecause='')

    def test_hotspot_physical_ports_are_same_internal_server(self):
        rows=[self.row(port='ether1'),self.row(port='ether2')]
        point=source_from_rows(rows,'hotspot')
        self.assertNotEqual(point['server_key'],'unknown')
        self.assertEqual(point['server_interface'],'')

    def test_pppoe_same_name_different_interface_distinct(self):
        a=source_from_rows([self.row(port='bridge1')],'pppoe')
        b=source_from_rows([self.row(port='bridge2')],'pppoe')
        self.assertNotEqual(a['server_key'],b['server_key'])
        self.assertEqual(source_from_rows([self.row(port='bridge1'),self.row(port='bridge2')],'pppoe')['server_key'],'unknown')

    def test_missing_or_ambiguous_is_unknown(self):
        for rows in ([],[self.row(nas='127.0.0.1')],[self.row(nas='')],[self.row(server='')],
                     [self.row(),self.row(server='HS2')],
                     [dict(self.row(),acctterminatecause='Consolidated-Historical-Import')]):
            self.assertEqual(source_from_rows(rows,'hotspot')['server_key'],'unknown')

    def test_identity_framing_prevents_concatenation_collisions(self):
        self.assertNotEqual(server_key('a','hotspot','bc'),server_key('ab','hotspot','c'))


class SortContracts(unittest.TestCase):
    def test_every_column_and_direction_uses_fixed_expressions(self):
        from services.sales_reporting_service import SORT_COLUMNS,sort_settings,sort_order
        for table,columns in SORT_COLUMNS.items():
            for key in columns:
                for direction in ('asc','desc'):
                    state=sort_settings({table+'_sort':key,table+'_dir':direction})
                    self.assertIn(direction.upper(),sort_order(table,state))
                    self.assertEqual(state[table],dict(key=key,direction=direction))

    def test_invalid_sort_never_enters_sql(self):
        from services.sales_reporting_service import sort_settings
        for args in ({'log_sort':'id;DROP TABLE x'},{'server_dir':'desc;--'},{'package_sort':'nope'}):
            with self.assertRaises(ValueError):sort_settings(args)

    def test_defaults_and_stable_pagination_ties(self):
        from services.sales_reporting_service import sort_settings,sort_order
        state=sort_settings({})
        self.assertEqual(state['log'],dict(key='date',direction='desc'))
        self.assertTrue(sort_order('log',state).endswith('e.event_at DESC,e.event_kind ASC,e.id DESC'))
        self.assertIn('(e.event_at IS NULL) ASC',sort_order('log',state))


if __name__=='__main__':unittest.main()
