"""Dashboard backup reminder: settings flags and authorization, no app startup."""
import unittest
from pathlib import Path
from jinja2 import Environment,FileSystemLoader
ROOT=Path(__file__).resolve().parents[1]
class BackupNotice(unittest.TestCase):
 def setUp(self):
  self.template=Environment(loader=FileSystemLoader(ROOT/'web/templates'),autoescape=True).get_template('components/backup_activation_notice.html')
 def render(self,value=None,missing=False,available=True,reseller=False,view=True,edit=True):
  return self.template.render(settings={} if missing else {'backup_auto_enabled':value},global_settings_available=available,is_reseller_scope=lambda:reseller,can_access_endpoint=lambda e:view if e=='backups_page' else edit,url_for=lambda e:'/backups')
 def test_disabled_or_missing_shows_notice(self):
  for value in [False,'false','0',0,'off',None]:
   with self.subTest(value=value):self.assertIn('id="backup-activation-notice"',self.render(value))
  self.assertIn('id="backup-activation-notice"',self.render(missing=True))
 def test_enabled_matches_backup_service_flag_format(self):
  for value in [True,1,'1','true','TRUE','yes','on']:
   with self.subTest(value=value):self.assertNotIn('backup-activation-notice',self.render(value))
 def test_no_notice_on_settings_read_failure(self):self.assertNotIn('backup-activation-notice',self.render(False,available=False))
 def test_reseller_never_sees_system_backup_notice(self):self.assertNotIn('backup-activation-notice',self.render(False,reseller=True))
 def test_permission_requires_both_page_and_settings_access(self):
  for view,edit in [(False,False),(False,True),(True,False)]:
   with self.subTest(view=view,edit=edit):self.assertNotIn('backup-activation-notice',self.render(False,view=view,edit=edit))
 def test_next_render_follows_current_setting(self):
  self.assertIn('backup-activation-notice',self.render('false'))
  self.assertNotIn('backup-activation-notice',self.render('true'))
  self.assertIn('backup-activation-notice',self.render('false'))
 def test_link_only_no_data_write_or_polling(self):
  html=self.render(False);self.assertIn('href="/backups"',html)
  self.assertNotIn('<script',html);self.assertNotIn('<form',html)
 def test_dashboard_includes_notice(self):self.assertIn("include 'components/backup_activation_notice.html'",(ROOT/'web/templates/dashboard.html').read_text(encoding='utf-8'))
if __name__=='__main__':unittest.main()
