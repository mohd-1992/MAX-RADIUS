"""Isolated API framing and shared-entry regressions; no workers or real DB."""
import ast
import inspect
import math
import socket
import struct
import hashlib
import types
import unittest
from contextlib import contextmanager
from decimal import Decimal
from functools import wraps
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]

def extract(path,names,ns):
    tree=ast.parse((ROOT/path).read_text(encoding='utf8'))
    nodes=[n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.ClassDef)) and n.name in names]
    exec(compile(ast.Module(body=nodes,type_ignores=[]),path,'exec'),ns)

class SharedPathTests(unittest.TestCase):
    def test_trap_is_drained_before_next_command(self):
        ns=dict(socket=socket,struct=struct,hashlib=hashlib)
        extract('core/mikrotik_api.py',{'RouterOSApiProtocol'},ns)
        api=ns['RouterOSApiProtocol']('fixture');sent=[]
        replies=iter([['!trap','=message=denied'],['!done'],['!re','=version=7'],['!done']])
        api.write_sentence=sent.append;api.read_sentence=lambda:next(replies)
        self.assertEqual(api.execute_command('/bad')[0]['message'],'denied')
        self.assertEqual(api.execute_command('/system/resource/print'),[{'version':'7'}])
        self.assertEqual(len(sent),2)

    def test_incomplete_reply_closes_socket(self):
        ns=dict(socket=socket,struct=struct,hashlib=hashlib)
        extract('core/mikrotik_api.py',{'RouterOSApiProtocol'},ns)
        for reply in ('eof','timeout','fatal'):
            api=ns['RouterOSApiProtocol']('fixture');closed=[]
            api.write_sentence=lambda _:None;api.close=lambda:closed.append(True)
            def read():
                if reply=='timeout':raise TimeoutError('fixture')
                return ['!fatal','=message=closed'] if reply=='fatal' else []
            api.read_sentence=read
            with self.subTest(reply=reply),self.assertRaises((ConnectionError,TimeoutError)):api.execute_command('/bad')
            self.assertTrue(closed)

    def fixture(self,kind='voucher',status='active',reward=None,points=100):
        calls=[];entity=dict(username='fixture',status=status)
        quick=types.ModuleType('services.quick_action_service')
        quick.get_target_entity=lambda *a:(entity,kind)
        parse=dict(Decimal=Decimal,math=math)
        extract('services/quick_action_service.py',{'parse_entitlement_adjustment','parse_quota_adjustment'},parse)
        quick.parse_entitlement_adjustment=parse['parse_entitlement_adjustment']
        online=types.ModuleType('services.online_policy_service');online.local=types.SimpleNamespace()
        @contextmanager
        def guard(user):calls.append('guard');yield
        def one(q,p=()):
            if 'wisp_subscribers' in q:return entity if kind=='subscriber' else None
            if 'wisp_vouchers' in q:return entity if kind=='voucher' else None
            if 'wisp_loyalty_rewards' in q:return reward
            if 'wisp_system_settings' in q:return {'value':'1'}
            if 'wisp_loyalty_wallets' in q:return {'points_balance':points}
            return None
        ns=dict(inspect=inspect,wraps=wraps,query_one=one,settled_account=guard,SettlementPending=RuntimeError)
        extract('services/renewal_settlement_service.py',{'settle_cycle_operation'},ns)
        return ns,quick,online,calls

    def test_card_loan_rejected_before_nas_and_body(self):
        ns,q,o,calls=self.fixture()
        def request_data_loan(username):calls.append('body');return False,'card'
        with patch.dict('sys.modules',{'services.quick_action_service':q,'services.online_policy_service':o}):
            self.assertFalse(ns['settle_cycle_operation'](request_data_loan)('fixture')[0])
        self.assertEqual(calls,[])

    def test_invalid_combined_topup_never_enters_guard(self):
        for days,quota in [(0,0),(-1,1),('nan',1),(1,'inf'),(1,'bad')]:
            ns,q,o,calls=self.fixture()
            def action_adjust_entitlement(entity_type,entity_id,days=0,quota_amount=0,quota_unit='MB'):calls.append('body');return True,'ok'
            with self.subTest(days=days,quota=quota),patch.dict('sys.modules',{'services.quick_action_service':q,'services.online_policy_service':o}):
                self.assertFalse(ns['settle_cycle_operation'](action_adjust_entitlement)('voucher',1,days,quota)[0])
            self.assertEqual(calls,[])

    def test_consumed_card_topup_never_enters_guard(self):
        ns,q,o,calls=self.fixture(status='recharged')
        def action_adjust_entitlement(entity_type,entity_id,days=0,quota_amount=0,quota_unit='MB'):calls.append('body');return True,'ok'
        with patch.dict('sys.modules',{'services.quick_action_service':q,'services.online_policy_service':o}):
            self.assertFalse(ns['settle_cycle_operation'](action_adjust_entitlement)('voucher',1,1,2)[0])
        self.assertEqual(calls,[])

    def test_time_quota_and_combined_entries_share_one_guard(self):
        for operation in ('time','quota','combined'):
            ns,q,o,calls=self.fixture()
            def action_adjust_entitlement(entity_type,entity_id,days=0,quota_amount=0,quota_unit='MB',admin_username='admin'):calls.append('body');return True,'ok'
            wrapped=ns['settle_cycle_operation'](action_adjust_entitlement)
            entries=dict(action_adjust_entitlement=wrapped)
            extract('services/quick_action_service.py',{'action_extend_time','action_add_quota'},entries)
            with self.subTest(operation=operation),patch.dict('sys.modules',{'services.quick_action_service':q,'services.online_policy_service':o}):
                result=entries['action_extend_time']('voucher',1,2) if operation=='time' else entries['action_add_quota']('voucher',1,-2,'MB') if operation=='quota' else wrapped('voucher',1,2,2)
                self.assertTrue(result[0])
            self.assertEqual(calls,['guard','body'])

    def test_ineligible_reward_does_not_disconnect(self):
        for active,points in [(0,100),(1,5)]:
            ns,q,o,calls=self.fixture(reward=dict(reward_type='data_bonus_mb',is_active=active,points_cost=20),points=points)
            def redeem_reward(username,reward_id):calls.append('body');return False,'ineligible'
            with patch.dict('sys.modules',{'services.quick_action_service':q,'services.online_policy_service':o}):
                self.assertFalse(ns['settle_cycle_operation'](redeem_reward)('fixture',1)[0])
            self.assertEqual(calls,['body'])

if __name__=='__main__':unittest.main(verbosity=2)
