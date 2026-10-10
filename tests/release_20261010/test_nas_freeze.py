import datetime
import sys
import types
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

db=types.ModuleType('database.db')
for name in ('get_connection','query_all','query_one'):setattr(db,name,lambda *a:None)
sys.modules['database']=types.ModuleType('database');sys.modules['database.db']=db
sys.modules['services']=types.ModuleType('services')
online=types.ModuleType('services.online_policy_service');online.seconds=lambda s:int(str(s).removesuffix('s'))
sys.modules['services.online_policy_service']=online
class Pending(RuntimeError):pass
settlement=types.ModuleType('services.renewal_settlement_service');settlement.SettlementPending=Pending
sys.modules['services.renewal_settlement_service']=settlement
sys.modules['core']=types.ModuleType('core')
api_module=types.ModuleType('core.mikrotik_api');sys.modules['core.mikrotik_api']=api_module
ns={'__name__':'freeze_test'}
exec(compile((Path(__file__).resolve().parents[2]/'services/nas_session_settlement.py').read_text(encoding='utf8'),'freeze','exec'),ns)

def snapshot():
    return {i:{'row':{'username':'test','nasipaddress':'127.0.0.1','radacctid':i,'acctsessionid':str(i),'framedipaddress':'10.1.0.'+str(i),'acctuniqueid':'unique-'+str(i),'acctstarttime':'2026-10-09 19:59:00'},
               'nas':{'api_port':8728,'api_username':'test','api_password':'test'},'kind':'hotspot',
               'live':{'.id':'*'+str(i),'user':'test','address':'10.1.0.'+str(i),'mac-address':'AA:00:00:00:00:0'+str(i),'radius':'true','bytes-in':'100','bytes-out':'200','uptime':'60s'}} for i in (1,2)}

class API:
    def __init__(self,**kw):
        self.live=[s['live'].copy() for s in snapshot().values()];self.rules=[];self.scheduled=[];self.fasttrack=False;self.unstable=False;self.refuse=False;self.seq=0
    def connect(self):pass
    def login(self,*a):return True
    def close(self):pass
    def execute_command(self,path,words=None):
        if path=='/ip/firewall/filter/print':return [{'action':'fasttrack-connection','disabled':'false'}] if self.fasttrack else []
        if path=='/system/clock/print':return [{'date':'2026-10-09','time':'20:00:00'}]
        if path.endswith('/add'):
            self.seq+=1;row=dict(words,**{'.id':'*r'+str(self.seq)})
            (self.rules if '/raw/' in path else self.scheduled).append(row);return [{'ret':row['.id']}]
        if path=='/ip/hotspot/active/print':
            if self.unstable:
                for r in self.live:r['bytes-in']=str(int(r['bytes-in'])+1)
            return [dict(r) for r in self.live]
        if path=='/ip/hotspot/active/remove':
            if self.refuse:return [{'!trap':'test reject'}]
            self.live=[r for r in self.live if r['.id']!=words['numbers']];return []
        rows=self.rules if '/raw/' in path else self.scheduled
        if path.endswith('/print'):
            if not words:return [dict(r) for r in rows]
            k,v=words[0][1:].split('=',1);return [dict(r) for r in rows if r.get(k)==v]
        if path.endswith('/remove'):
            rows[:]=[r for r in rows if r['.id']!=words['numbers']];return []
        raise AssertionError(path)

class PPPAPI(API):
    def __init__(self):
        super().__init__()
        self.ppp=[dict(r,name=r['user'],**{'caller-id':r['mac-address'],'session-id':'0x'+str(i)}) for i,r in enumerate(self.live,1)]
        self.live=[];self.bad_stats=False
    def execute_command(self,path,words=None):
        if path=='/ppp/active/print':return [dict(r) for r in self.ppp]
        if path=='/interface/pppoe-server/print':return [{'.id':'*if'+r['.id'],'remote-address':r['caller-id'],'running':'true'} for r in self.ppp]
        if path=='/interface/print':return [] if self.bad_stats else [{'.id':'*if'+r['.id'],'rx-byte':'100','tx-byte':'200'} for r in self.ppp]
        if path=='/ppp/active/remove':
            if self.refuse:return [{'!trap':'rejected'}]
            self.ppp=[r for r in self.ppp if r['.id']!=words['numbers']];return []
        return super().execute_command(path,words)

class FreezeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.env=patch.dict(os.environ,MAX_SETTLEMENT_JOURNAL_DIR=self.temp.name);self.env.start()
    def tearDown(self):self.env.stop();self.temp.cleanup()
    def run_case(self,api,seal):
        data=snapshot()
        with patch.object(api_module,'RouterOSApiProtocol',create=True,side_effect=lambda *a,**k:api),patch.dict(ns,seal_rows=seal):
            ns['freeze_and_disconnect'](data)
        return data
    def test_all_sessions_absent_before_seal_and_cleanup(self):
        api=API()
        def seal(rows):
            self.assertEqual(api.live,[]);self.assertEqual(len(api.rules),4);self.assertEqual(len(rows),2)
        data=self.run_case(api,seal);self.assertEqual(data,{});self.assertEqual(api.rules,[]);self.assertEqual(api.scheduled,[])
    def test_fasttrack_prevents_billing_and_disconnect(self):
        api=API();api.fasttrack=True
        with self.assertRaises(Pending):self.run_case(api,lambda _:self.fail('must not seal'))
        self.assertEqual(len(api.live),2);self.assertEqual(api.rules,[])
    def test_disconnect_rejection_no_seal_rules_removed(self):
        api=API();api.refuse=True
        with self.assertRaises(Pending):self.run_case(api,lambda _:self.fail('must not seal'))
        self.assertEqual(len(api.rules),4);self.assertEqual(len(api.scheduled),1);self.assertEqual(len(api.live),2)
        self.assertEqual(len(list(Path(self.temp.name).glob('*.json'))),1)
    def test_unstable_counters_never_accepted(self):
        api=API();api.unstable=True
        with patch.object(ns['time'],'sleep',return_value=None),self.assertRaises(Pending):self.run_case(api,lambda _:self.fail('must not seal'))
        self.assertEqual(len(api.live),2);self.assertEqual(api.rules,[])
    def test_seal_failure_does_not_mark_success(self):
        api=API()
        with self.assertRaises(Pending):self.run_case(api,lambda _:(_ for _ in ()).throw(RuntimeError('test transaction failure')))
        self.assertEqual(len(api.rules),4);self.assertEqual(len(api.scheduled),1)
        self.assertEqual(len(list(Path(self.temp.name).glob('*.json'))),1)
    def test_detached_journal_recovers_without_rebilling(self):
        api=API();api.live=[];data=snapshot()
        for s in data.values():s.update(input=100,output=200,seconds=60,evidence_token='a'*32)
        path=ns['journal'](data,'detached');seen=[]
        with patch.object(api_module,'RouterOSApiProtocol',create=True,side_effect=lambda *a,**k:api),patch.dict(ns,seal_rows=lambda rows:seen.append(len(rows)),query_one=lambda *a:{'api_port':8728,'api_username':'test','api_password':'test'}):
            ns['recover_journals']('test')
        self.assertEqual(seen,[2]);self.assertFalse(path.exists())
    def test_expired_freeze_proof_cannot_invent_counters(self):
        api=API();api.live=[];data=snapshot()
        for s in data.values():s.update(input=100,output=200,seconds=60,evidence_token='a'*32)
        path=ns['journal'](data,'frozen')
        with patch.object(api_module,'RouterOSApiProtocol',create=True,side_effect=lambda *a,**k:api),patch.dict(ns,seal_rows=lambda _:self.fail('must not seal'),query_one=lambda *a:{'api_port':8728,'api_username':'test','api_password':'test'}),self.assertRaises(RuntimeError):
            ns['recover_journals']('test')
        self.assertTrue(path.exists())

    def test_ppp_target_only_frozen_sealed_other_session_preserved(self):
        api=PPPAPI();data=snapshot();target={1:data[1]};target[1]['kind']='pppoe';target[1]['live']=api.ppp[0].copy()
        target[1]['row']['callingstationid']=api.ppp[0]['caller-id']
        def seal(rows):
            self.assertEqual([r['.id'] for r in api.ppp],['*2'])
            self.assertEqual((rows[1]['input'],rows[1]['output']),(100,200))
        with patch.object(api_module,'RouterOSApiProtocol',create=True,side_effect=lambda *a,**k:api),patch.dict(ns,seal_rows=seal):ns['freeze_and_disconnect'](target,include_ppp=True)
        self.assertEqual(target,{});self.assertEqual(api.rules,[])
    def test_ppp_missing_counters_rejected_before_disconnect(self):
        api=PPPAPI();api.bad_stats=True;data=snapshot();data[1]['kind']='pppoe';data[1]['live']=api.ppp[0].copy()
        with patch.object(api_module,'RouterOSApiProtocol',create=True,side_effect=lambda *a,**k:api),patch.dict(ns,seal_rows=lambda _:self.fail('must not seal')):
            with self.assertRaises(Pending):ns['freeze_and_disconnect']({1:data[1]},include_ppp=True)
        self.assertEqual(len(api.ppp),2);self.assertEqual(api.rules,[])
    def test_ppp_session_generation_mismatch_rejected(self):
        api=PPPAPI();data=snapshot();data[1]['kind']='pppoe';data[1]['live']=api.ppp[0].copy();data[1]['row']['acctsessionid']='9999'
        with patch.object(api_module,'RouterOSApiProtocol',create=True,side_effect=lambda *a,**k:api),patch.dict(ns,seal_rows=lambda _:self.fail('must not seal')):
            with self.assertRaises(Pending):ns['freeze_and_disconnect']({1:data[1]},include_ppp=True)
        self.assertEqual(len(api.ppp),2)

if __name__=='__main__':unittest.main(verbosity=2)
