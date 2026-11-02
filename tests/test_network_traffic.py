"""Pure network report contracts; no app factory, DB or router."""
import datetime as dt
import unittest
from services.network_traffic_service import selected_filters,sessions_sql,point_info,sum_rows,format_bytes


class NetworkTraffic(unittest.TestCase):
    def test_today_default_and_invalid_filters(self):
        self.assertEqual(selected_filters({})['period'],'today')
        for args in ({'period':'bad'},{'nas':'x OR 1=1'},{'service_type':'both'},
                     {'server':'bad'},{'traffic_sort':'total;--'},{'traffic_dir':'DESC'},
                     {'period':'custom','date_from':'2026-02-30'}):
            with self.assertRaises(ValueError):selected_filters(args)

    def test_scope_dates_and_archival_identity(self):
        start=dt.datetime(2026,10,26);end=dt.datetime(2026,10,27)
        sql,params=sessions_sql(7,start,end)
        self.assertEqual(params,(7,7,start,end)*3+(7,start,end))
        self.assertEqual(sql.count('acctstarttime>=%s'),4)
        self.assertEqual(sql.count('acctstarttime<%s'),4)
        self.assertIn('radacct_archive_owned',sql)
        self.assertIn('wisp_imported_session_history_owned',sql)
        self.assertIn('wisp_traffic_history_owned',sql)
        self.assertIn('acctuniqueid,username,acctsessionid,nasipaddress,acctstarttime,fallback_id',sql)
        self.assertNotIn('wisp_voucher_sales',sql)

    def test_hotspot_ports_merge_pppoe_interfaces_stay_distinct(self):
        base=dict(nas_address='192.0.2.10',service_type='hotspot',server_name='HS')
        a=point_info(dict(base,server_interface='ether1'),{})
        b=point_info(dict(base,server_interface='ether2'),{})
        self.assertEqual(a['server_key'],b['server_key'])
        a=point_info(dict(base,service_type='pppoe',server_interface='bridge1'),{})
        b=point_info(dict(base,service_type='pppoe',server_interface='bridge2'),{})
        self.assertNotEqual(a['server_key'],b['server_key'])

    def test_unknowns_scoped_by_nas_and_kind(self):
        a=point_info(dict(nas_address='192.0.2.10',service_type='pppoe'),{'192.0.2.10':'Router'})
        b=point_info(dict(nas_address='192.0.2.11',service_type='pppoe'),{})
        self.assertEqual(a['nas_label'],'Router')
        self.assertFalse(a['point_known'])
        self.assertNotEqual(a['server_key'],b['server_key'])
        self.assertEqual(point_info(dict(nas_address='127.0.0.1'),{})['nas_address'],'')

    def test_unknown_is_not_zero_and_partial_totals_keep_coverage(self):
        result=sum_rows([dict(sessions=1,download=None,upload=None,unknown_download=1,unknown_upload=1)])
        self.assertIsNone(result['total'])
        result=sum_rows([dict(sessions=1,download=None,upload=None,unknown_download=1),
                         dict(sessions=1,download=0,upload=10)])
        self.assertEqual(result['download'],0)
        self.assertEqual(result['total'],10)
        self.assertEqual(result['unknown_download'],1)
        self.assertEqual(sum_rows([])['total'],0)

    def test_recorded_name_survives_missing_service_without_guessing_protocol(self):
        a=point_info(dict(nas_address='192.0.2.10',service_type='',server_name='HS-A',server_interface='ether1'),{})
        b=point_info(dict(nas_address='192.0.2.10',service_type='',server_name='HS-B',server_interface='ether1'),{})
        same=point_info(dict(nas_address='192.0.2.10',service_type='',server_name='HS-A',server_interface='ether2'),{})
        self.assertEqual(a['server_label'],'HS-A')
        self.assertEqual(a['service_type'],'')
        self.assertFalse(a['point_known'])
        self.assertNotEqual(a['server_key'],b['server_key'])
        self.assertEqual(a['server_key'],same['server_key'])

    def test_partial_named_point_filter_and_totals_agree(self):
        from services.network_traffic_service import matches_point,sum_rows
        rows=[dict(username='fixture',nas_address='192.0.2.10',service_type='',server_name=name,
                   sessions=1,download=down,upload=up) for name,down,up in [('A',100,10),('B',300,30)]]
        point=point_info(rows[0],{})
        selected=dict(nas='',service_type='unknown',server=point['server_key'])
        self.assertTrue(matches_point(point,selected))
        self.assertFalse(matches_point(point_info(rows[1],{}),selected))
        totals=sum_rows([r for r in rows if matches_point(point_info(r,{}),selected)])
        self.assertEqual((totals['total'],totals['sessions']),(110,1))
        selected['server']=''
        self.assertEqual(sum_rows([r for r in rows if matches_point(point_info(r,{}),selected)])['total'],440)

    def test_invalid_point_name_stays_unknown(self):
        point=point_info(dict(nas_address='192.0.2.10',service_type='',server_name='bad'+chr(127)),{})
        self.assertEqual(point['server_label'],'غير محدد')

    def test_byte_units(self):
        self.assertEqual(format_bytes(None),'غير متاح')
        self.assertEqual(format_bytes(0),'0 B')
        self.assertEqual(format_bytes(2**30),'1.00 GiB')


if __name__=='__main__': unittest.main()
