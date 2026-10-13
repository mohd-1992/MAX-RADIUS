"""Deduction UI and ledger receipts against the isolated manager fixture."""
import sys
import unittest
from unittest.mock import patch
from flask import Response
from manager_page_fixture import fixture

class WalletReceipts(unittest.TestCase):
 def setUp(self):
  self.app,self.db,self.ns,self.stubs,_,_=fixture()
  self.ns['Response']=Response
  self.client=self.app.test_client()
  with self.client.session_transaction() as s:s.update(admin_id=1,admin_username='audit1',permissions=['managers.billing','managers.view'],_license_csrf='test-token')
 def tearDown(self):self.db.close()
 def post(self,amount=20,reason='سحب نقدي',notes='',token='test-token'):
  with patch.dict(sys.modules,self.stubs):return self.client.post('/managers/2/deduct',data=dict(amount=amount,reason=reason,notes=notes,csrf_token=token,request_id='receipt_deduction_reference_0123'))
 def balance(self):return self.ns['query_one']('SELECT wallet_balance FROM wisp_managers WHERE id=2')['wallet_balance']
 def test_deduction_reference_once_and_reason_recorded(self):
  self.assertEqual(self.post().status_code,302);self.assertEqual(self.post().status_code,302)
  self.assertEqual(self.balance(),80)
  invoices=self.ns['query_all']('SELECT * FROM wisp_manager_invoices')
  self.assertEqual(len(invoices),1);self.assertEqual(invoices[0]['notes'],'سحب نقدي')
 def test_invalid_reason_amount_and_csrf_do_not_change_balance(self):
  for values in [dict(reason=''),dict(reason='أخرى'),dict(amount=101),dict(amount=-1),dict(amount='NaN'),dict(token='wrong')]:self.post(**values)
  self.assertEqual(self.balance(),100)
 def test_receipt_read_and_render_do_not_change_ledger(self):
  self.post(notes='<script>bad()</script>')
  invoice=self.ns['query_one']('SELECT * FROM wisp_manager_invoices')
  for _ in range(2):
   response=self.client.get('/managers/invoices/%s/receipt'%invoice['id'])
   self.assertEqual(response.status_code,200)
   self.assertIn('walletReceipt',response.text);self.assertIn('80.00',response.text)
   self.assertIn('&lt;script&gt;bad()',response.text)
  self.assertEqual(self.balance(),80)
  self.assertEqual(len(self.ns['query_all']('SELECT * FROM wisp_manager_invoices')),1)
 def test_missing_receipt_and_denied_permission(self):
  self.assertEqual(self.client.get('/managers/invoices/999/receipt').status_code,404)
  with self.client.session_transaction() as s:s['permissions']=[]
  self.assertNotEqual(self.client.get('/managers/invoices/999/receipt').status_code,200)
 def test_deduction_modal_is_rendered_inside_content(self):
  response=self.client.get('/managers')
  self.assertEqual(response.status_code,200)
  self.assertIn('id="deductModal"',response.text);self.assertIn('openDeductModal',response.text)

if __name__=='__main__':unittest.main()
