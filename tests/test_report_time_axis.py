import datetime as dt
import unittest
from services.report_time_axis import axis, sales_series, bucket_index
from services.traffic_sample_service import series

NOW=dt.datetime(2026,10,27,9,tzinfo=dt.timezone.utc)


class ReportAxes(unittest.TestCase):
    def test_calendar_windows(self):
        for period,mode,count in [('today','hour',24),('yesterday','hour',24),('this_week','day',7),('this_month','day',31),('last_month','day',30),('this_year','month',12)]:
            actual,windows,_=axis({'period':period},NOW)
            self.assertEqual((actual,len(windows)),(mode,count))
        self.assertEqual(len(axis({'period':'this_month'},dt.datetime(2024,2,12,tzinfo=dt.timezone.utc))[1]),29)

    def test_timezone_and_exclusive_boundaries(self):
        _,windows,_=axis({'period':'today'},NOW,'Asia/Dubai')
        self.assertEqual(bucket_index(dt.datetime(2026,10,26,23),windows,'Asia/Dubai'),0)
        self.assertIsNone(bucket_index(dt.datetime(2026,10,27,23),windows,'Asia/Dubai'))

    def test_empty_open_ended_historical_range(self):
        mode,windows,_=axis({'period':'custom','date_to':'2020-01-01'},NOW)
        self.assertEqual(len(windows),24)
        self.assertEqual(windows[0][1].date(),dt.date(2020,1,1))

    def test_week_is_seven_observed_dates_ending_today(self):
        from services.sales_reporting_service import date_bounds
        for day in (26,27):
            now=dt.datetime(2026,10,day,9,tzinfo=dt.timezone.utc)
            _,windows,clock=axis({'period':'this_week'},now)
            self.assertEqual(len(windows),7)
            self.assertEqual(windows[0][1].date(),clock.date()-dt.timedelta(days=6))
            self.assertEqual(windows[-1][1].date(),clock.date())
            start,end=date_bounds('this_week',now=now)
            self.assertEqual((end-start).days,7)
            self.assertEqual(end.date(),clock.date()+dt.timedelta(days=1))

    def test_money_exact_and_future_not_zero(self):
        def query(*args):return [dict(bucket=1,amount=300)]
        data=sales_series(query,'ledger','1=1',(),{'period':'today'},NOW)
        self.assertEqual(sum(v or 0 for v in data['sales']),300)
        self.assertEqual(data['sales'][1],300)
        self.assertIsNone(data['sales'][23])

    def test_missing_invoice_cost_is_not_a_zero_sale_on_dashboard(self):
        from services.dashboard_reporting_service import sales_series as dashboard_sales
        def query(*args):return [dict(bucket='2026-10',event_kind='invoice',total=None,unknown_amounts=1)]
        result=dashboard_sales(query,now=NOW)
        self.assertIsNone(result['package_sales'][-1]);self.assertIsNone(result['sales'][-1])
        self.assertEqual(result['unknown_amounts'],1);self.assertEqual(result['voucher_sales'][-1],0)

    def test_counter_reset_gap_and_direction_independence(self):
        from services.traffic_sample_service import delta
        self.assertEqual(delta(dict(download=100,upload=10),dict(download=150,upload=20),60),(50,10,0))
        self.assertEqual(delta(dict(download=100,upload=10),dict(download=50,upload=20),60),(None,10,1))
        self.assertEqual(delta(dict(download=100,upload=None),dict(download=150,upload=20),60),(50,None,1))
        self.assertEqual(delta(dict(download=100,upload=10),dict(download=150,upload=20),600),(50,10,0))

    def test_open_custom_start_extends_through_today(self):
        mode,windows,_=axis(dict(period='custom',date_from='2026-10-20'),NOW)
        self.assertEqual((mode,len(windows)),('day',8))
        self.assertEqual(windows[-1][0],'2026-10-27')

    def test_future_start_does_not_show_a_different_day(self):
        mode,windows,_=axis(dict(period='custom',date_from='2026-11-01'),NOW)
        self.assertEqual(windows[0][1].date(),dt.date(2026,11,1))
        self.assertEqual(len(windows),24)

    def test_permission_and_point_filters(self):
        seen=[]
        def query(sql,params=()):
            seen.append((sql,params))
            return [dict(epoch='x',marker='x')] if 's.epoch' in sql else []
        result=series(query,{'period':'today','nas':'192.0.2.2'},owner=7,now=NOW)
        self.assertIn('owner_scope=%s',seen[-1][0]);self.assertIn(7,seen[-1][1])
        self.assertTrue(all(v is None for v in result['download']))


if __name__=='__main__':unittest.main()
