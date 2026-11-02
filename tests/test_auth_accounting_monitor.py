"""Synthetic observations only: no MAX imports, SQL, SSH, Docker or NAS access."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]


def load(name):
    path=ROOT/'deploy/hosted-max'/f'{name}.py'
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


engine=load('auth_accounting_monitor')
collector=load('auth_accounting_collect')


def snap(epoch=10000):
    return {'schema':1,'tenant':'m','instance':'INST-SYNTHETIC','epoch':epoch,
        'coverage':{a:{'complete':True,'reason':'complete'} for a in ('auth','sessions','accounts','operations','nas')},
        'sessions':[],'accounts':[],'auth':[],'operations':[],'nas_sessions':[],'interim_seconds':60}


def session(**kw):
    row={'id':1,'user':'dummy','nas':'192.0.2.1','sid':'1234','uid':'unique',
        'class':'maxcap:same','mac':'00:11:22:33:44:55','ip':'192.0.2.10',
        'start':9000,'updated':9950,'stop':None,'input':10,'output':20,'seconds':950,
        'cycle':8900,'has_baseline':False,'cause':''}
    row.update(kw);return row


def account(**kw):
    row={'id':1,'kind':'subscriber','user':'dummy','status':'active','cycle':8900,
        'expires':20000,'quota_mb':100,'extra_mb':0,'used_bytes':30,'loan':0,
        'balance_cents':1000,'package_id':1,'simultaneous':2,'uncertain':False}
    row.update(kw);return row


def codes(s,previous=None):return {x['code'] for x in engine.assess(s,previous)}


class SilentFaults(unittest.TestCase):
    def test_clean_snapshot(self):self.assertEqual(codes(snap()),set())

    def test_counter_regression_without_any_error_log(self):
        old=snap(9700);old['sessions']=[session(input=100)]
        new=snap();new['sessions']=[session(input=90)]
        self.assertIn('counter_regressed',codes(new,old))

    def test_large_64_bit_progress_is_normal(self):
        old=snap(9700);old['sessions']=[session(input=2**40)]
        new=snap();new['sessions']=[session(input=2**40+2**32)]
        self.assertNotIn('counter_regressed',codes(new,old))

    def test_identity_mutation(self):
        old=snap(9700);old['sessions']=[session()]
        new=snap();new['sessions']=[session(uid='changed')]
        self.assertIn('accounting_identity_changed',codes(new,old))

    def test_cycle_unknown_does_not_mean_zero(self):
        s=snap();s['accounts']=[account(used_bytes=None,uncertain=True)]
        self.assertIn('accounting_unknown',codes(s))
        self.assertNotIn('quota_exhausted_still_online',codes(s))

    def test_crossing_closed_session_still_needs_baseline(self):
        s=snap();s['sessions']=[session(start=8000,cycle=8500,stop=9500)]
        self.assertIn('missing_cycle_baseline',codes(s))
        s['sessions'][0]['has_baseline']=True
        self.assertNotIn('missing_cycle_baseline',codes(s))

    def test_old_session_ended_before_cycle_is_excluded(self):
        s=snap();s['sessions']=[session(start=8000,stop=8400,cycle=8500)]
        self.assertNotIn('missing_cycle_baseline',codes(s))

    def test_stop_is_not_invented_for_watchdog_close(self):
        s=snap();s['sessions']=[session(stop=9500,cause='Watchdog-Autoheal-Timeout')]
        self.assertIn('nonfinal_session_close',codes(s))
        for cause in ('User-Request','NAS-Verified-Closed'):
            s['sessions'][0]['cause']=cause
            self.assertNotIn('nonfinal_session_close',codes(s))

    def test_interim_interval_not_fixed_at_five_minutes(self):
        s=snap();s['sessions']=[session(updated=9000)];s['interim_seconds']=900
        self.assertNotIn('accounting_update_stale',codes(s))
        s['interim_seconds']=60
        self.assertIn('accounting_update_stale',codes(s))

    def test_same_sid_on_different_routers_is_not_duplicate(self):
        s=snap();s['sessions']=[session(),session(id=2,nas='192.0.2.2')]
        self.assertNotIn('possible_split_session',codes(s))
        s['sessions'][1]['nas']='192.0.2.1'
        self.assertIn('possible_split_session',codes(s))

    def test_different_class_is_a_distinct_session(self):
        s=snap();s['sessions']=[session(),session(id=2,**{'class':'another'})]
        self.assertNotIn('possible_split_session',codes(s))

    def test_only_explicitly_checked_rows_can_be_missing(self):
        old=snap(9700);old['sessions']=[session()]
        s=snap();self.assertNotIn('accounting_row_missing',codes(s,old))
        s['checked_session_ids']=[1]
        self.assertIn('accounting_row_missing',codes(s,old))

    def test_silent_entitlement_change_is_a_review_candidate(self):
        old=snap(9700);old['accounts']=[account()]
        s=snap();s['accounts']=[account(balance_cents=500)]
        self.assertIn('unexplained_entitlement_change',codes(s,old))
        s['operations']=[{'id':1,'kind':'audit','user':'dummy','at':9800,'action':'ADD_WALLET'}]
        self.assertNotIn('unexplained_entitlement_change',codes(s,old))

    def test_legitimate_negative_extra_quota_or_money_is_not_auto_fault(self):
        s=snap();s['accounts']=[account(extra_mb=-50,balance_cents=-200)]
        self.assertEqual(codes(s),set())

    def test_regressed_cycle_usage_without_cycle_change(self):
        old=snap(9700);old['accounts']=[account(used_bytes=100)]
        s=snap();s['accounts']=[account(used_bytes=10)]
        self.assertIn('cycle_consumption_regressed',codes(s,old))
        s['accounts'][0]['cycle']=9900
        self.assertNotIn('cycle_consumption_regressed',codes(s,old))

    def test_operation_receipt_without_result(self):
        old=snap(9700);old['accounts']=[account()]
        s=snap();s['accounts']=[account()]
        s['operations']=[{'id':4,'kind':'audit','user':'dummy','action':'RENEW_PACKAGE','at':9710}]
        self.assertIn('operation_without_cycle_transition',codes(s,old))

    def test_multiple_renewal_sales_for_same_voucher_are_valid(self):
        s=snap();s['operations']=[{'kind':'sale','id':i,'voucher_id':9,'first_activation':None} for i in (1,2)]
        self.assertNotIn('duplicate_first_activation_sale',codes(s))
        for op in s['operations']:op['first_activation']=9
        self.assertIn('duplicate_first_activation_sale',codes(s))

    def test_duplicate_invoice_id_retransmission_is_not_double_charge(self):
        s=snap();s['operations']=[{'kind':'invoice','id':1,'number':'INV1'}]*2
        self.assertNotIn('duplicate_invoice_number',codes(s))
        s['operations'].append({'kind':'invoice','id':2,'number':'INV1'})
        self.assertIn('duplicate_invoice_number',codes(s))


class AuthenticationAndNAS(unittest.TestCase):
    def live(self,**kw):
        item={'nas':'192.0.2.1','kind':'hotspot','user':'dummy','ip':'192.0.2.10',
              'mac':'00:11:22:33:44:55','sid':None,'radius':True,'uptime':500,'input':100,'output':100}
        item.update(kw);return item

    def test_missing_start_is_not_immediate_fault(self):
        s=snap();s['auth']=[{'id':1,'user':'dummy','reply':'Access-Accept','at':9990}]
        self.assertNotIn('accept_without_accounting_evidence',codes(s))
        s['auth'][0]['at']=9700
        self.assertIn('accept_without_accounting_evidence',codes(s))

    def test_current_stop_can_supply_accounting_evidence(self):
        s=snap();s['auth']=[{'id':1,'user':'dummy','reply':'Access-Accept','at':9700}]
        s['sessions']=[session(stop=9990)]
        self.assertNotIn('accept_without_accounting_evidence',codes(s))

    def test_no_automatic_guess_of_reject_reason(self):
        s=snap();s['auth']=[{'id':i,'reply':'Access-Reject','user':'dummy','at':9900} for i in range(25)]
        item=next(x for x in engine.assess(s) if x['code']=='authentication_reject_surge')
        self.assertIn('مشروعة',item['message'])

    def test_blocked_accept_needs_two_stable_account_observations(self):
        old=snap(9700);old['accounts']=[account(status='suspended')]
        s=snap();s['accounts']=[account(status='suspended')]
        s['auth']=[{'id':1,'user':'dummy','reply':'Access-Accept','at':9900}]
        self.assertIn('accept_for_blocked_account',codes(s,old))
        old['accounts'][0]['status']='active'
        self.assertNotIn('accept_for_blocked_account',codes(s,old))

    def test_local_non_radius_nas_accounts_are_excluded(self):
        s=snap();s['nas_sessions']=[self.live(radius=False)]
        self.assertNotIn('live_without_open_accounting',codes(s))

    def test_live_connection_and_sql_match_without_sid(self):
        s=snap();s['sessions']=[session()];s['nas_sessions']=[self.live()]
        self.assertNotIn('live_without_open_accounting',codes(s))
        s['sessions'][0]['mac']='another'
        self.assertIn('live_without_open_accounting',codes(s))

    def test_expired_and_exhausted_live_connection(self):
        s=snap();s['accounts']=[account(expires=9000,quota_mb=1,used_bytes=2*1048576)]
        s['nas_sessions']=[self.live()]
        self.assertIn('expired_account_still_online',codes(s))
        self.assertIn('quota_exhausted_still_online',codes(s))

    def test_nas_failure_does_not_claim_disconnection(self):
        s=snap();s['coverage']['nas']={'complete':False,'reason':'TimeoutError'}
        s['sessions']=[session(updated=9000)]
        self.assertNotIn('open_accounting_absent_on_nas',codes(s))
        self.assertIn('observation_unavailable',codes(s))


class IncidentsAndSafety(unittest.TestCase):
    def test_unknown_tenant_or_cross_tenant_state_rejected(self):
        for bad in ('as','max','shn','m; rm','MAX1'):
            s=snap();s['tenant']=bad
            with self.assertRaises(ValueError):engine.assess(s)
            with self.assertRaises(ValueError):collector.ReadOnlyDB(bad)
        other=snap(9700);other['tenant']='max1'
        with self.assertRaises(ValueError):engine.assess(snap(),other)

    def test_replayed_snapshot_rejected(self):
        with self.assertRaises(ValueError):engine.assess(snap(),snap())

    def test_notify_once_then_two_complete_observations_to_recover(self):
        s=snap();s['accounts']=[account(uncertain=True)]
        state,r=engine.advance(s);self.assertEqual(len(r['notifications']),1)
        s['epoch']+=300
        state,r=engine.advance(s,state);self.assertEqual(r['notifications'],[])
        s['accounts'][0]['uncertain']=False;s['epoch']+=300
        state,r=engine.advance(s,state);self.assertEqual(r['notifications'],[])
        s['epoch']+=300
        state,r=engine.advance(s,state)
        self.assertEqual([n['event'] for n in r['notifications']],['recovered'])

    def test_failed_observation_does_not_clear_incident(self):
        s=snap();s['accounts']=[account(uncertain=True)]
        state,_=engine.advance(s)
        for i in range(3):
            s['epoch']+=300;s['accounts']=[]
            s['coverage']['accounts']={'complete':False,'reason':'query_failed'}
            state,r=engine.advance(s,state)
            self.assertFalse(any(x['event']=='recovered' for x in r['notifications']))
        self.assertTrue(any(x['code']=='accounting_unknown' for x in state['incidents'].values()))

    def test_missing_snapshot_keeps_last_valid_counter_baseline(self):
        s=snap();s['sessions']=[session(input=100)]
        state,_=engine.advance(s)
        failed=snap(10300);failed['coverage']['sessions']={'complete':False,'reason':'timeout'}
        state,_=engine.advance(failed,state)
        new=snap(10600);new['sessions']=[session(input=90)]
        _,report=engine.advance(new,state)
        self.assertIn('counter_regressed',{x['code'] for x in report['findings']})

    def test_deferred_deep_pass_is_not_a_network_incident(self):
        s=snap();s['coverage']['nas']={'complete':False,'reason':'deferred_until_deep_pass'}
        self.assertNotIn('observation_unavailable',codes(s))

    def test_deep_recovery_can_span_a_light_pass(self):
        s=snap();s['accounts']=[account(uncertain=True)]
        state,_=engine.advance(s)
        s['epoch']+=300;s['accounts'][0]['uncertain']=False
        state,_=engine.advance(s,state)
        s['epoch']+=300;s['coverage']['nas']={'complete':False,'reason':'deferred_until_deep_pass'}
        state,_=engine.advance(s,state)
        s['epoch']+=300;s['coverage']['nas']={'complete':True,'reason':'complete'}
        _,r=engine.advance(s,state)
        self.assertTrue(any(x['event']=='recovered' for x in r['notifications']))

    def test_gap_is_visible(self):
        self.assertIn('observation_gap',codes(snap(),snap(8000)))

    def test_historical_financial_fault_never_clears_when_it_leaves_window(self):
        s=snap();s['operations']=[{'id':i,'kind':'invoice','number':'X'} for i in (1,2)]
        state,_=engine.advance(s)
        for i in range(3):
            s['epoch']+=300;s['operations']=[];state,r=engine.advance(s,state)
        self.assertTrue(any(x['code']=='duplicate_invoice_number' for x in state['incidents'].values()))

    def test_atomic_output(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'state.json';engine.atomic_json(p,{'ok':True})
            self.assertEqual(json.loads(p.read_text()),{'ok':True})
            self.assertFalse(p.with_name(p.name+'.tmp').exists())

    def test_sql_guard_and_read_only_transaction(self):
        db=collector.ReadOnlyDB('m')
        for sql in ('UPDATE x SET a=1','SELECT 1;DELETE FROM x','SELECT SLEEP(3)',
                    'SELECT x INTO OUTFILE \'/tmp/x\'','SELECT x FROM y FOR UPDATE'):
            with self.assertRaises(ValueError):db.query(sql)
        with patch.object(collector.subprocess,'run') as run:
            run.return_value.returncode=0;run.return_value.stdout='{"ok":1}\n'
            self.assertEqual(db.query("SELECT JSON_OBJECT('ok',1)"),[{'ok':1}])
            args=run.call_args.args[0]
            self.assertEqual(args[2],'max_radius_m_db')
            self.assertIn('START TRANSACTION READ ONLY;',args[-1])
            self.assertTrue(args[-1].endswith(';ROLLBACK;'))

    def test_sql_literal_cannot_escape_into_code(self):
        value="x'; DROP DATABASE foo; --"
        encoded=collector.literal(value)
        self.assertNotIn(';',encoded);self.assertIn(value.encode().hex(),encoded)
        self.assertIn('COLLATE utf8mb4_unicode_ci',encoded)

    def test_nas_identity_change_prevents_connection(self):
        config={'nas':{'192.0.2.1':8728}}
        creds=[{'ip':'192.0.2.2','port':8728,'username':'dummy','password':'dummy'}]
        with patch.object(collector.socket,'create_connection') as connect:
            out=collector.nas_observation(config,creds)
            connect.assert_not_called();self.assertTrue(out['errors'])

    def test_router_control_commands_cannot_be_sent(self):
        api=collector.RouterReadOnly('192.0.2.1',8728,'dummy','dummy')
        for command in ('/ip/hotspot/active/remove','/system/reboot','/radius/set','/ppp/active/remove'):
            with self.assertRaises(ValueError):api.read(command)

    def test_duration(self):
        for value,expected in [('15m',900),('1h2m3s',3723),('1d01:02:03',90123),('00:02:00',120),('0',0),('bad',None)]:
            self.assertEqual(collector.duration(value),expected)

    def test_partial_binary_packets_and_trap_are_drained(self):
        wire=b''.join(collector.RouterReadOnly.encode(w) for w in ('!trap','=message=denied'))+b'\0'+collector.RouterReadOnly.encode('!done')+b'\0'+collector.RouterReadOnly.encode('!done')+b'\0'
        class Sock:
            def __init__(self):self.data=wire
            def sendall(self,unused):pass
            def settimeout(self,unused):pass
            def recv(self,n):
                value=self.data[:min(n,2)];self.data=self.data[len(value):];return value
        import time
        api=collector.RouterReadOnly('192.0.2.1',8728,'dummy','dummy');api.sock=Sock();api.deadline=time.monotonic()+1
        with self.assertRaises(PermissionError):api.exchange(['/read'])
        self.assertEqual(api.exchange(['/read']),([],{}))

    def test_hotspot_full_read_keeps_credentials_out_of_result(self):
        class API:
            def __init__(self,*unused):pass
            def __enter__(self):return self
            def __exit__(self,*unused):pass
            def read(self,command):
                return [{'user':'dummy','radius':'true','uptime':'5m','password':'NEVER_COPY_THIS'}] if command=='/ip/hotspot/active/print' else []
        with patch.object(collector,'RouterReadOnly',API):
            r=collector.nas_observation({'nas':{'192.0.2.1':8728}},[{'ip':'192.0.2.1','port':8728,'username':'dummy','password':'PASSWORD_VALUE'}])
        self.assertFalse(r['errors'])
        self.assertNotIn('NEVER_COPY_THIS',json.dumps(r));self.assertNotIn('PASSWORD_VALUE',json.dumps(r))

    def test_no_application_imports_or_background_scheduler(self):
        import ast
        for name in ('auth_accounting_monitor','auth_accounting_collect'):
            tree=ast.parse((ROOT/'deploy/hosted-max'/f'{name}.py').read_text(encoding='utf-8'))
            modules=[n.module for n in ast.walk(tree) if isinstance(n,ast.ImportFrom)]
            self.assertFalse(any(m and m.startswith(('web','core','database','services')) for m in modules))
            self.assertFalse(any(isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr in ('start','set_timer','schedule') for n in ast.walk(tree)))


if __name__=='__main__':unittest.main()
