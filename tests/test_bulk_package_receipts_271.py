"""Request identity survives the legacy bulk route without changing old callers."""
import ast
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from flask import Flask, request, jsonify
ROOT=Path(__file__).resolve().parents[1]
class BulkPackageReceiptTests(unittest.TestCase):
 def setUp(self):
  source=ROOT/'web/routes/system.py';tree=ast.parse(source.read_text(encoding='utf-8'));node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='api_change_package');node.decorator_list=[];self.ids=[11,22];self.kind='subscriber';self.engine=Mock(return_value=(True,'local accepted'));self.actions=[]
  def bulk(action,kind,ids,**options):
   self.actions.append(action)
   for ident in dict.fromkeys(int(x) for x in ids):action(kind,ident,**options)
   return True,'local batch accepted'
  self.bulk=Mock(side_effect=bulk);env=dict(request=request,jsonify=jsonify,_get_target_params=lambda:(self.kind,self.ids),action_change_package=self.engine,action_bulk_execute=self.bulk,get_current_manager=lambda:dict(username='local-admin'));exec(compile(ast.Module(body=[node],type_ignores=[]),str(source),'exec'),env);self.route=env['api_change_package'];self.app=Flask('isolated-bulk-receipt-test')
 def call(self,token=None):
  data=dict(new_package_id='13')
  if token is not None:data['operation_id']=token
  with self.app.test_request_context('/api/actions/change-package',method='POST',data=data):return self.route()
 def test_same_request_stable_per_target_after_reorder_and_duplicate_ids(self):
  self.call('a'*32);first={c.args[1]:c.kwargs['operation_id'] for c in self.engine.call_args_list};self.engine.reset_mock();self.ids=['22','11','11'];self.call('a'*32);second={c.args[1]:c.kwargs['operation_id'] for c in self.engine.call_args_list};self.assertEqual(first,second);self.assertEqual(len(set(first.values())),2);self.assertTrue(all(len(v)==32 for v in first.values()))
 def test_distinct_entity_kind_cannot_share_the_same_child_receipt(self):
  self.ids=[11];self.call('b'*32);old=self.engine.call_args.kwargs['operation_id'];self.kind='voucher';self.call('b'*32);self.assertNotEqual(old,self.engine.call_args.kwargs['operation_id'])
 def test_invalid_reference_rejected_before_any_batch_or_account_effect(self):
  response,status=self.call('invalid-local');self.assertEqual(status,400);self.bulk.assert_not_called();self.engine.assert_not_called()
 def test_legacy_missing_reference_keeps_existing_engine_contract(self):
  self.call();self.assertIs(self.actions[0],self.engine);self.assertTrue(all('operation_id' not in c.kwargs for c in self.engine.call_args_list))
if __name__=='__main__':unittest.main()
