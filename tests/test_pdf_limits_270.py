"""Real isolated renderer and cancellation tests; no DB or application workers."""
import os,tempfile,time,unittest,unicodedata
from pathlib import Path
from unittest.mock import patch
from services.pdf_export_service import render_parts
from services.heavy_operation_service import ExportCancelled
from pypdf import PdfReader

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
