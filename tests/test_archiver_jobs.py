"""Isolated archive planning/journal guards; no application or DB is started."""
import datetime as dt
import unittest
from unittest.mock import patch,Mock
from services import accounting_archiver_service as archive,maintenance_job_service as jobs,expired_voucher_purge as purge


class ArchiveOptions(unittest.TestCase):
    def test_invalid_options_cannot_start_work(self):
        for days,mode in ((0,'archive'),(-1,'summary'),(3651,'archive'),(90,'unknown')):
            with self.subTest(days=days,mode=mode),self.assertRaises(ValueError):
                archive._validate_archive_options(days,mode)

    def test_preview_uses_selected_age_and_preserves_unknown_failure(self):
        with patch('core.license_feature_policy.require'),patch('database.db.query_one',return_value={'n':17}) as read:
            result=archive.get_archive_plan(30)
            self.assertEqual(result['count'],17)
            self.assertEqual(read.call_args.args[1],(30,))
            read.side_effect=RuntimeError('unavailable')
            with self.assertRaises(RuntimeError):archive.get_archive_plan(90)

    def test_summary_is_a_candidate_count_with_one_cutoff(self):
        cutoff=dt.datetime(2026,1,1)
        with patch('core.license_feature_policy.require'),patch('database.db.query_one',side_effect=[{'cutoff':cutoff},{'n':5}]) as read:
            result=archive.get_archive_plan(90,'summary')
        self.assertEqual(result['unit'],'cards')
        self.assertFalse(result['exact_at_read'])
        self.assertTrue(all(value==cutoff for value in read.call_args.args[1]))
        self.assertIn('COLLATE utf8mb4_unicode_ci',read.call_args.args[0])


class JournalRead(unittest.TestCase):
    def test_kind_is_bound_and_stale_observation_is_read_only(self):
        row=dict(job_id='a'*32,state='running',parameters='{"job_kind":"accounting-archive"}',progress='{}',created_at='fixture',updated_at='fixture')
        with patch.object(jobs,'ensure_job_table'),patch.object(jobs,'query_one',side_effect=[row,{'stale':1}]) as read,patch.object(jobs,'execute_update') as update:
            result=jobs.get_maintenance_job('accounting-archive',row['job_id'])
        self.assertEqual(result['state'],'interrupted')
        self.assertEqual(read.call_args_list[0].args[1],(row['job_id'],'accounting-archive'))
        update.assert_not_called()

    def test_invalid_kind_or_cancel_id_is_rejected_before_writes(self):
        with patch.object(jobs,'execute_update') as update:
            with self.assertRaises(ValueError):jobs.get_maintenance_job('factory-reset')
            with self.assertRaises(ValueError):jobs.cancel_maintenance_job('../invalid','accounting-archive')
        update.assert_not_called()


class ExistingSummaryGuards(unittest.TestCase):
    def assert_physical_deferral(self, reason, *, seal=None, cycle_crossing=False):
        now=dt.datetime(2027,2,4)
        row=dict(radacctid=1,acctuniqueid='a'*32,acctsessionid='fixture',
                 acctstarttime=now-dt.timedelta(days=151),acctstoptime=now-dt.timedelta(days=150),
                 acctterminatecause='User-Request',acctinputoctets=200,
                 acctoutputoctets=800,acctsessiontime=1800)
        account=dict(id=1,username='fixture',status='expired',
                     last_renewed_at=now-dt.timedelta(days=150,hours=12) if cycle_crossing else now-dt.timedelta(days=200))
        def read(conn,statement,*args):
            self.assertTrue(statement.startswith('SELECT'),'Deferral must precede every write')
            if statement.startswith('SELECT username'):return []
            if statement.startswith('SELECT * FROM wisp_account_usage_seals'):return [seal] if seal else []
            return None
        with patch.object(purge,'sql',side_effect=read),patch.object(purge,'locked_details',return_value=[row]):
            result=purge.purge_details(object(),account,history_only=True,cutoff=now-dt.timedelta(days=90))
        self.assertTrue(result['deferred']);self.assertEqual(result['reason'],reason)

    def test_another_accounts_uid_seal_is_never_adopted(self):
        self.assert_physical_deferral('ambiguous_owner',seal=dict(acctuniqueid='a'*32,voucher_id=2,input_bytes=200,output_bytes=800,uptime_seconds=1800))

    def test_positive_late_delta_is_retained_for_reconciliation(self):
        self.assert_physical_deferral('late_summary_delta',seal=dict(acctuniqueid='a'*32,voucher_id=1,input_bytes=200,output_bytes=700,uptime_seconds=1800))

    def test_session_crossing_cycle_anchor_is_retained(self):
        self.assert_physical_deferral('unsettled_or_incomplete_history',cycle_crossing=True)

    def test_active_card_cannot_be_summarized_by_shared_engine(self):
        with patch.object(purge,'sql') as sql:
            result=purge.purge_details(object(),{'id':1,'status':'active'},history_only=True,cutoff=dt.datetime.now())
        self.assertEqual(result['reason'],'account_not_expired');sql.assert_not_called()

    def test_unique_imported_history_is_not_added_to_live_quota(self):
        def read(conn,statement,*args):
            self.assertTrue(statement.startswith('SELECT'))
            if statement.startswith('SELECT username'):return []
            return None
        with patch.object(purge,'sql',side_effect=read),patch.object(purge,'locked_details',side_effect=[[],[],[{'radacctid':2}]]):
            result=purge.purge_details(object(),{'id':1,'username':'fixture','status':'expired'},history_only=True,cutoff=dt.datetime.now())
        self.assertEqual(result['reason'],'history_outside_live_quota')


if __name__=='__main__':unittest.main()
