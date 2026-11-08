"""Real isolated renderer and cancellation tests; no DB or application workers."""
import os,tempfile,time,unittest,unicodedata,ast,io
from pathlib import Path
from unittest.mock import patch
from services.pdf_export_service import render_parts
from services.heavy_operation_service import ExportCancelled
from pypdf import PdfReader
from services.batch_export_service import iter_card_pdf_groups


class CardPdfGroups(unittest.TestCase):
 def test_936_cards_keep_24_original_sheets(self):
  groups=list(iter_card_pdf_groups(list(range(936)),39))
  self.assertEqual(len(groups),6)
  self.assertTrue(all(len(cards)==156 and len(pages)==4 for cards,pages in groups))
  self.assertEqual([c for _,pages in groups for page in pages for c in page],list(range(936)))
  self.assertEqual([len(p) for _,pages in groups for p in pages],[39]*24)
 def test_last_incomplete_sheet_is_not_padded_or_lost(self):
  groups=list(iter_card_pdf_groups(list(range(937)),39))
  self.assertEqual(groups[-1],([936],[[936]]))
 def test_default_design_keeps_48_card_boundaries(self):
  self.assertEqual([len(cards) for cards,_ in iter_card_pdf_groups(list(range(101)))],[48,48,5])
 def test_large_original_sheet_is_not_enlarged(self):
  for size in (80,156,200):
   groups=list(iter_card_pdf_groups(list(range(501)),size))
   self.assertTrue(all(len(cards)<=max(size,156) for cards,_ in groups))
   self.assertEqual([c for _,pages in groups for page in pages for c in page],list(range(501)))
 def test_invalid_sheet_and_worker_limits_fail_before_start(self):
  for size in (0,-1,True,1.5,'39'):
   with self.assertRaises(ValueError):list(iter_card_pdf_groups([1],size))
  for size in (0,9,True,1.5,'8',None):
   with self.assertRaises(ValueError):render_parts(iter([]),'.',parts_per_worker=size)


class CardPdfRoute(unittest.TestCase):
 """Exercise the actual route body without DB, workers, or a real export."""
 def setUp(self):
  from flask import Flask,request,current_app,jsonify,send_file
  import services.pdf_export_service as renderer
  self.app=Flask('pdf-route-contract',static_folder=str(Path(__file__).resolve().parents[1]/'web/static'))
  self.cards=[{'id':i} for i in range(936)];self.calls=[];self.options={};self.owned=True
  def query(sql,args=()):
   if 'COUNT(*)' in sql:return {'n':len(self.cards)}
   if 'wisp_system_settings' in sql:return None
   return {'id':1} if self.owned else None
  def template(name,**values):self.calls.append(values);return 'bounded fixture HTML'
  def render(parts,assets,**kw):self.options=kw;list(parts);return io.BytesIO(b'fixture PDF')
  scope={'request':request,'current_app':current_app,'jsonify':jsonify,'send_file':send_file,'query_one':query,'get_batch_cards_for_print':lambda identity:({'name':'fixture'},self.cards,{}),'get_card_design_by_id':lambda identity:{'cards_per_row':3,'cards_per_col':13},'get_all_card_designs':lambda:[],'render_template':template}
  root=Path(__file__).resolve().parents[1];node=next(n for n in ast.parse((root/'web/routes/vouchers.py').read_text(encoding='utf-8-sig')).body if isinstance(n,ast.FunctionDef) and n.name=='print_cards');node.decorator_list=[];exec(compile(ast.Module(body=[node],type_ignores=[]),'card-pdf-route','exec'),scope);self.app.add_url_rule('/print/<int:batch_id>','print_cards',scope['print_cards']);self.client=self.app.test_client();patcher=patch.object(renderer,'render_parts',side_effect=render);patcher.start();self.addCleanup(patcher.stop)
 def test_custom_pdf_has_six_groups_without_losing_a_card(self):
  response=self.client.get('/print/1?export=pdf&design_id=4');self.assertEqual(response.status_code,200);response.close()
  self.assertEqual(len(self.calls),6);self.assertEqual(self.options['parts_per_worker'],8);self.assertEqual(self.options['timeout'],600)
  self.assertEqual([len(p) for call in self.calls for p in call['card_pages']],[39]*24)
  self.assertEqual([c['id'] for call in self.calls for c in call['cards']],list(range(936)))
 def test_browser_print_view_remains_one_render_with_24_sheets(self):
  response=self.client.get('/print/1?design_id=4');self.assertEqual(response.status_code,200);response.close();self.assertEqual(len(self.calls),1);self.assertEqual(len(self.calls[0]['card_pages']),24);self.assertEqual(self.options,{})
 def test_default_design_keeps_original_chunk_boundaries(self):
  self.cards=self.cards[:49];response=self.client.get('/print/1?export=pdf');self.assertEqual(response.status_code,200);response.close();self.assertEqual([len(c['cards']) for c in self.calls],[48,1])
 def test_ownership_and_card_limit_still_block_before_render(self):
  self.owned=False;response=self.client.get('/print/1?export=pdf');self.assertEqual(response.status_code,404);self.assertEqual(self.calls,[])
  self.owned=True;self.cards=[{}]*2001;response=self.client.get('/print/1?export=pdf');self.assertEqual(response.status_code,400);self.assertEqual(self.calls,[])

class PdfLimits(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
  env=patch.dict(os.environ,STORAGE_DIR=self.temp.name);env.start();self.addCleanup(env.stop)
  self.assets=str(Path(__file__).resolve().parents[1]/'web/static')
 def html(self,text):return '<html><head></head><body><p>'+text+'</p></body></html>'
 def test_complete_ordered_pdf_parts(self):
  with render_parts((self.html('fixture-part-'+str(i)) for i in range(2)),self.assets) as file:
   reader=PdfReader(file)
   self.assertEqual(len(reader.pages),2)
   self.assertIn('fixture-part-0',unicodedata.normalize('NFKC',reader.pages[0].extract_text()))
   self.assertIn('fixture-part-1',unicodedata.normalize('NFKC',reader.pages[1].extract_text()))
 def test_recycled_groups_keep_order_and_isolate_page_scripts(self):
  def pages():
   for i in range(9):
    yield self.html('ordered-'+str(i))+"<script>if(window.previousCard)document.body.innerHTML='LEAK';window.previousCard=true;</script>"
  with render_parts(pages(),self.assets) as file:
   reader=PdfReader(file)
   self.assertEqual(len(reader.pages),9)
   for i,page in enumerate(reader.pages):
    self.assertIn('ordered-'+str(i),page.extract_text())
    self.assertNotIn('LEAK',page.extract_text())
 def test_failure_after_recycled_group_never_returns_partial_pdf(self):
  parts=[self.html('valid-'+str(i)) for i in range(4)]
  parts.append(self.html('<img src="file:///etc/passwd">'))
  with self.assertRaises(RuntimeError):render_parts(iter(parts),self.assets)
  with render_parts(iter([self.html('after-failure')]),self.assets) as file:
   self.assertIn('after-failure',PdfReader(file).pages[0].extract_text())
 def test_eight_part_workers_keep_order_and_fresh_page_state(self):
  parts=(self.html('group-eight-'+str(i))+"<script>if(window.previousCard)document.body.innerHTML='LEAK';window.previousCard=true;</script>" for i in range(9))
  with render_parts(parts,self.assets,parts_per_worker=8) as file:
   reader=PdfReader(file);self.assertEqual(len(reader.pages),9)
   for i,page in enumerate(reader.pages):
    self.assertIn('group-eight-'+str(i),page.extract_text());self.assertNotIn('LEAK',page.extract_text())
 def test_failure_after_eight_parts_discards_the_whole_job(self):
  parts=[self.html('valid-'+str(i)) for i in range(8)]+[self.html('<img src="https://blocked.invalid/not-local.png">')]
  with self.assertRaises(RuntimeError):render_parts(iter(parts),self.assets,parts_per_worker=8)
  with render_parts(iter([self.html('eight-recovery')]),self.assets,parts_per_worker=8) as file:
   self.assertEqual(len(PdfReader(file).pages),1)
 def test_page_producer_failure_releases_admission_lock(self):
  def broken():
   yield self.html('uncommitted')
   raise ValueError('fixture page producer failed')
  with self.assertRaisesRegex(ValueError,'producer failed'):
   render_parts(broken(),self.assets)
  with render_parts(iter([self.html('after-producer-failure')]),self.assets) as file:
   self.assertEqual(len(PdfReader(file).pages),1)
 def test_cancel_during_worker_then_next_export_succeeds(self):
  calls=[0]
  def cancel():calls[0]+=1;return calls[0]>=3
  with self.assertRaises(ExportCancelled):
   render_parts(iter([self.html('cancelled')]),self.assets,cancelled=cancel)
  with render_parts(iter([self.html('recovered')]),self.assets) as file:
   self.assertIn('recovered',PdfReader(file).pages[0].extract_text())
 def test_deadline_and_memory_limit_are_explicit_failures(self):
  with self.assertRaises(TimeoutError):render_parts(iter([self.html('timeout')]),self.assets,timeout=.01)
  with self.assertRaisesRegex(RuntimeError,'ميزانية'):
   render_parts(iter([self.html('memory')]),self.assets,memory_mb=1)
 def test_failed_part_never_returns_partial_pdf(self):
  with self.assertRaises(RuntimeError):
   render_parts(iter([self.html('first'),self.html('<img src="https://blocked.invalid/private.png">')]),self.assets)
 def test_running_browser_cancel_and_normal_completion_leave_no_processes(self):
  def browsers():
   result=[]
   for proc in Path('/proc').glob('[0-9]*'):
    try:
     if any(name in (proc/'comm').read_text().lower() for name in ('chrom','node')):
      result.append(proc.name)
    except OSError:pass
   return result
  seen=[False]
  def cancel():
   seen[0]=seen[0] or bool(browsers())
   return seen[0]
  with self.assertRaises(ExportCancelled):
   render_parts(iter([self.html('cancel running browser')]),self.assets,cancelled=cancel,timeout=25)
  self.assertTrue(seen[0])
  self.assertEqual(browsers(),[])
  with render_parts(iter([self.html('complete after cancel')]),self.assets):pass
  self.assertEqual(browsers(),[])
 def test_older_linux_without_pidfd_still_reaps_renderer_children(self):
  import subprocess,sys
  source=Path(self.temp.name)/'source.html';target=Path(self.temp.name)/'complete.pdf'
  source.write_text(self.html('legacy-kernel'),encoding='utf-8')
  code="import os,errno,sys\ndef unsupported(*args):raise OSError(errno.ENOSYS,'fixture')\nos.pidfd_open=unsupported\nfrom services.pdf_export_service import render_worker\nrender_worker(*sys.argv[1:])"
  subprocess.run([sys.executable,'-B','-c',code,str(source),str(target),self.assets],check=True,timeout=30,start_new_session=True)
  self.assertEqual(len(PdfReader(target).pages),1)

if __name__=='__main__':unittest.main()
