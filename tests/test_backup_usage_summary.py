"""Backup aggregation protects complete identities and never edits the source DB."""
import datetime as dt
import io
import unittest
from tests.test_backup_compaction import table
from services.backup_usage_summary import summarize_dump
from services.backup_compaction import _events,_rows

COLS=('radacctid','acctuniqueid','acctsessionid','username','account_username','account_read_username',
      'account_subscriber_id','account_voucher_id','acctstarttime','acctstoptime','acctterminatecause',
      'acctinputoctets','acctoutputoctets','acctsessiontime','nasipaddress')


def row(i,**changes):
    value=dict(zip(COLS,(i,format(i,'032x'),'s'+str(i),'old',None,'current',None,1,
        '2026-11-01 12:00:00','2026-11-01 13:00:00','User-Request',100,200,3600,'192.0.2.1')))
    value.update(changes)
    return tuple(value[k] for k in COLS)


def snapshot(rows,baseline=(),reservations=(),history=(),status='expired'):
    return (table('radacct',COLS,rows)+
        table('radacct_archive',COLS,history)+
        table('wisp_vouchers',('id','username','status','last_renewed_at','reseller_id'),((1,'current',status,'2026-10-31',7),))+
        table('wisp_subscribers',('id','username'))+
        table('wisp_session_baselines',('radacctid',),baseline)+
        table('wisp_session_reservations',('username','expires_at'),reservations)+
        table('financial_test',('id','amount'),((1,98765),)))


def run(raw):
    f=io.BytesIO(raw);receipt=summarize_dump(f,dt.datetime(2026,11,3,12))
    kept=[]
    for t,c,r in _events(f):
        if t=='radacct':kept.extend(x for _,x in _rows(r,c))
    return receipt,f.getvalue(),kept


class SummaryTests(unittest.TestCase):
    def test_closed_expired_rename_ownership_and_money(self):
        original=snapshot([row(1),row(2)])
        receipt,out,kept=run(original)
        self.assertEqual(receipt['summarized_sessions'],2)
        self.assertEqual(receipt['summary_rows'],1)
        self.assertFalse(kept)
        self.assertIn(table('financial_test',('id','amount'),((1,98765),)),out)
        self.assertIn(b'wisp_account_usage_seals',out)
        self.assertIn(b'200,400,7200,2)',out)

    def test_any_unsettled_row_protects_entire_card(self):
        for changes in ({'acctstoptime':None},{'acctterminatecause':'Backup-Restored-Closed'},
                        {'acctinputoctets':None},{'acctinputoctets':-1},{'acctstarttime':None},
                        {'acctuniqueid':'invalid'},{'acctsessionid':''}):
            receipt,_,kept=run(snapshot([row(1),row(2,**changes)]))
            self.assertEqual(receipt['summarized_sessions'],0,changes)
            self.assertEqual(len(kept),2)

    def test_baseline_live_reservation_or_copied_history_protects(self):
        for kwargs in ({'baseline':((1,),)},
                       {'reservations':(('current','2026-11-03 12:01:00'),)},
                       {'history':(row(99),)}):
            receipt,_,kept=run(snapshot([row(1)],**kwargs))
            self.assertEqual(receipt['summarized_sessions'],0)
            self.assertEqual(len(kept),1)

    def test_active_card_never_summarized(self):
        receipt,_,kept=run(snapshot([row(1)],status='active'))
        self.assertEqual(receipt['summarized_sessions'],0)
        self.assertEqual(len(kept),1)

    def test_duplicate_session_ids_protect(self):
        receipt,_,kept=run(snapshot([row(1),row(2,acctsessionid='s1')]))
        self.assertEqual(receipt['summarized_sessions'],0)
        self.assertEqual(len(kept),2)

    def test_cycle_day_and_nas_are_separate(self):
        receipt,_,_=run(snapshot([row(1,acctstarttime='2026-10-30 12:00:00',acctstoptime='2026-10-30 13:00:00'),
            row(2),row(3,nasipaddress='192.0.2.2')]))
        self.assertEqual(receipt['summary_rows'],3)

    def test_nas_counter_evidence_keeps_its_foreign_key_session(self):
        raw=snapshot([row(1)])+table('wisp_nas_session_seals',('radacctid',),((1,),))
        receipt,_,kept=run(raw)
        self.assertEqual(receipt['summarized_sessions'],0)
        self.assertEqual(len(kept),1)

    def test_consolidated_import_keeps_quota_but_is_hidden_from_report(self):
        receipt,out,_=run(snapshot([row(1,acctterminatecause='Consolidated-Historical-Import')]))
        self.assertEqual(receipt['summarized_sessions'],1)
        self.assertIn(b"'',0,100,200,3600,1)",out)

    def test_duplicate_unique_id_protects_whole_card(self):
        receipt,_,kept=run(snapshot([row(1),row(2,acctuniqueid=format(1,'032x'))]))
        self.assertEqual(receipt['summarized_sessions'],0)
        self.assertEqual(len(kept),2)

    def test_late_cross_card_uid_conflict_discards_already_staged_groups(self):
        fields=('id','username','status','last_renewed_at','reseller_id')
        raw=snapshot([row(1),row(2,account_voucher_id=2,account_read_username='other',
                                     acctuniqueid=format(1,'032x'))])
        old=table('wisp_vouchers',fields,((1,'current','expired','2026-10-31',7),))
        new=table('wisp_vouchers',fields,((1,'current','expired','2026-10-31',7),
                                         (2,'other','expired','2026-10-31',8)))
        receipt,_,kept=run(raw.replace(old,new))
        self.assertEqual(receipt['summarized_sessions'],0)
        self.assertEqual(receipt['summary_rows'],0)
        self.assertEqual(receipt['protected_cards'],2)
        self.assertEqual(len(kept),2)

    def test_failure_does_not_replace_original(self):
        raw=snapshot([row(1,acctinputoctets=10**22)]);f=io.BytesIO(raw)
        with self.assertRaises(OverflowError):summarize_dump(f,dt.datetime(2026,11,3))
        self.assertEqual(f.getvalue(),raw)


if __name__=='__main__':unittest.main()
