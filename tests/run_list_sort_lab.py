"""Read-only list HTTP checks on the existing isolated synthetic fixture only."""
import os
import sys
import threading
import unittest
from unittest.mock import patch

assert sys.argv[1:]==['--run'] and os.environ.get('DB_NAME')=='sales266_fixture'
threading.Thread.start=lambda self:None
import services.license_guard_service as lic
active=lambda *a,**k:dict(valid=True,status='active',features={},limits={},plan_tier='Synthetic')
lic.get_active_license_status=active
lic.has_license_feature=lambda *a,**k:True
import core.license_feature_policy as feature
feature.verified_state=active
from flask import template_rendered
from web import create_app
from services.list_sort_service import FIELDS, sort_state, sort_rows, card_order
from database.db import query_all
import database.db as db
# A missing optional display field must fail the check, never trigger healing.
db._run_with_autoheal=lambda func,*args,**kwargs:func(*args,**kwargs)

app=create_app({'TESTING':True,'SALES_SOURCE_ENRICHMENT':False})


class ListHTTP(unittest.TestCase):
    def setUp(self):
        self.client=app.test_client()
        with self.client.session_transaction() as session:
            session['admin_id']=1
        self.sync=patch('web.routes.vouchers.sync_voucher_activations')
        self.sync.start()
        self.addCleanup(self.sync.stop)

    def get(self,path,**args):
        contexts=[]
        def capture(sender,template,context,**extra): contexts.append(context)
        with template_rendered.connected_to(capture,app):
            response=self.client.get(path,query_string=dict(status='all',**args))
        self.assertEqual(response.status_code,200)
        self.assertIn('list-sort.js',response.get_data(as_text=True))
        self.assertIn('aria-sort=',response.get_data(as_text=True))
        return contexts[-1]

    def test_every_subscriber_and_batch_heading_both_directions(self):
        for table,path,field in [('subscribers','/subscribers','subscribers'),('batches','/vouchers','batches')]:
            for key in FIELDS[table]:
                for direction in ('asc','desc'):
                    context=self.get(path,list_sort=key,list_dir=direction)
                    rows=context[field]
                    expected=sort_rows(rows,sort_state({'list_sort':key,'list_dir':direction},table))
                    self.assertEqual([r['id'] for r in rows],[r['id'] for r in expected])

    def test_subscriber_route_with_virtual_rows(self):
        # Process-local records also cover empty-subscriber fixtures without seeds.
        rows=[dict(id=i,seq_id=i,username='virtual'+str(i),full_name=name,
                   days_remaining_num=9999,is_expired=False,is_online=False,
                   last_seen_str='لم يتصل بعد',has_quota=False,consumption_percent=0,
                   balance=0,service_type='pppoe',package_name='Virtual',accounting_available=False)
              for i,name in [(1,'Beta'),(2,'Alpha'),(3,'Gamma')]]
        with patch('web.routes.subscribers.get_subscribers',return_value=rows):
            context=self.get('/subscribers',list_sort='fullname',list_dir='asc')
        self.assertEqual([r['id'] for r in context['subscribers']],[2,1,3])

    def test_cards_sql_matches_full_order_before_limit(self):
        for key in FIELDS['cards']:
            for direction in ('asc','desc'):
                state=sort_state({'list_sort':key,'list_dir':direction},'cards')
                sql=card_order(state)
                if not sql: continue
                expected=query_all("SELECT v.id FROM wisp_vouchers v JOIN wisp_packages p ON v.package_id=p.id JOIN wisp_voucher_batches b ON v.batch_id=b.id LEFT JOIN wisp_resellers r ON v.reseller_id=r.id WHERE v.status IN ('active','used','expired','recharged','suspended','disabled') ORDER BY "+sql)
                context=self.get('/vouchers/active-users',list_sort=key,list_dir=direction)
                self.assertEqual([r['id'] for r in context['active_cards']],[r['id'] for r in expected[:50]])
                if len(expected)>50:
                    second=self.get('/vouchers/active-users',list_sort=key,list_dir=direction,page=2)
                    self.assertEqual([r['id'] for r in second['active_cards']],[r['id'] for r in expected[50:100]])

    def test_derived_cards_sort_complete_result_before_paging(self):
        for key in ('days','lastseen','accesspoint','consumption','ip','notes'):
            for direction in ('asc','desc'):
                captured=[]
                def capture(rows,state):
                    result=sort_rows(rows,state)
                    captured.append(result)
                    return result
                with patch('services.list_sort_service.sort_rows',side_effect=capture):
                    context=self.get('/vouchers/active-users',list_sort=key,list_dir=direction)
                self.assertEqual(len(captured[0]),context['total_records'])
                self.assertEqual([r['id'] for r in context['active_cards']],[r['id'] for r in captured[0][:50]])


if __name__=='__main__':
    unittest.main(argv=[sys.argv[0]])
