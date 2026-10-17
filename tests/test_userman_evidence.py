"""Pure evidence checks: no app imports, NAS, or customer data."""
import copy
import datetime as dt
import unittest
from services.userman_import_evidence import historical_evidence


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.user = dict(username='fixture', first_used_at='2026-10-01 10:00:00',
                         expires_at='2026-11-01 10:00:00', upload_used_bytes=100,
                         download_used_bytes=200, uptime_used_seconds=90,
                         source_sessions=[dict(session_id='session', nas='source-alias',
                            mac='AA:BB:CC:00:00:01', ip='192.0.2.5', start='2026-10-01 10:00:00',
                            stop='2026-10-01 10:01:30', upload=100, download=200, uptime=90)])
        self.row = dict(radacctid=1, acctsessionid='session', nasipaddress='192.0.2.1',
                        callingstationid='aa:bb:cc:00:00:01', framedipaddress='192.0.2.5',
                        acctstarttime=dt.datetime(2026,10,1,10), acctstoptime=dt.datetime(2026,10,1,10,2),
                        acctinputoctets=150, acctoutputoctets=250, acctsessiontime=120)

    def check(self, rows=None):
        return historical_evidence(self.user, rows or [], {'source-alias':'192.0.2.1'})

    def test_fresh_account_needs_no_fabricated_dates(self):
        self.assertIsNone(historical_evidence({}, [])['review'])

    def test_shared_partial_is_subtracted_and_actual_unchanged(self):
        before=copy.deepcopy(self.row)
        self.assertEqual(self.check([self.row]),dict(review=None,residual=(0,0,0)))
        self.assertEqual(self.row,before)

    def test_historical_only_and_repeat_are_deterministic(self):
        self.assertEqual(self.check()['review'],'unconfirmed_target_cutover')
        self.assertEqual(self.check(),self.check())

    def test_missing_original_date_is_not_inferred_from_uptime(self):
        self.user.pop('first_used_at')
        self.assertEqual(self.check()['review'],'missing_original_dates')

    def test_aggregate_only_requires_review(self):
        self.user.pop('source_sessions')
        self.assertEqual(self.check()['review'],'missing_session_evidence')

    def test_wrong_identity_unmatched_target_requires_review(self):
        for field in ('acctsessionid','nasipaddress','callingstationid','framedipaddress','acctstarttime'):
            with self.subTest(field=field):
                row=copy.deepcopy(self.row);row[field]=dt.datetime(2026,10,2) if field=='acctstarttime' else 'wrong'
                self.assertEqual(self.check([row])['review'],'unconfirmed_target_cutover')

    def test_counter_regression_open_target_and_crossing_are_rejected(self):
        for field,value in [('acctinputoctets',99),('acctoutputoctets',199),('acctsessiontime',89),
                            ('acctstoptime',None),('acctstarttime',dt.datetime(2026,10,1,9,59,59))]:
            with self.subTest(field=field):
                row=copy.deepcopy(self.row);row[field]=value
                self.assertEqual(self.check([row])['review'],'unsettled_or_crossing_session')

    def test_duplicate_session_and_ambiguous_target_rejected(self):
        self.assertEqual(self.check([self.row,dict(self.row,radacctid=2)])['review'],'ambiguous_session_match')
        self.user['source_sessions']*=2
        self.assertEqual(self.check([self.row])['review'],'invalid_session_evidence')

    def test_incomplete_profile_totals_rejected(self):
        self.user['download_used_bytes']=201
        self.assertEqual(self.check([self.row])['review'],'incomplete_cycle_history')

    def test_open_source_requires_settled_match(self):
        self.user['source_sessions'][0]['stop']=None
        self.assertEqual(self.check()['review'],'unsettled_source_session')
        self.assertIsNone(self.check([self.row])['review'])

    def test_cycle_must_cover_all_history(self):
        self.user['last_renewed_at']='2026-10-02 10:00:00'
        self.assertEqual(self.check()['review'],'invalid_session_evidence')

    def test_negative_totals_rejected(self):
        self.user['upload_used_bytes']=-1
        with self.assertRaises(ValueError):self.check()

    def test_previous_cycle_rows_are_not_lifetime_consumption(self):
        old=dict(self.row,radacctid=2,acctsessionid='old',acctstarttime=dt.datetime(2026,9,1),
                 acctstoptime=dt.datetime(2026,9,2),acctinputoctets=999999999)
        self.assertIsNone(self.check([self.row,old])['review'])

    def test_explicit_zero_seconds_is_not_replaced_by_rounded_minutes(self):
        self.user['uptime_used_seconds']=0;self.user['uptime_used_mins']=1
        self.user['source_sessions'][0]['uptime']=0
        self.assertIsNone(self.check([self.row])['review'])


if __name__=='__main__':unittest.main()
