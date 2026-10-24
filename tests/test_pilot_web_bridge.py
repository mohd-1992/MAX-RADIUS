"""Native MAX bridge has no generic Docker/feed access or Flask request shadow."""
import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

PATH=Path(__file__).resolve().parents[1]/'deploy/hosted-max/pilot_web_bridge.py'


class BridgeTests(unittest.TestCase):
    def setUp(self):
        core=types.ModuleType('core');config=types.ModuleType('core.config');config.APP_VERSION='2.6.2'
        with patch.dict(sys.modules,{'core':core,'core.config':config}):
            spec=importlib.util.spec_from_file_location('pilot_bridge',PATH);self.m=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.m)
    def test_no_flask_request_name_export(self):self.assertNotIn('request',{k for k in vars(self.m) if not k.startswith('_')})
    def test_only_scoped_manifest_is_available(self):
        response=dict(success=True,has_update=True,latest_version='2.6.3',target_domain='4k.max-net.net',components=['web','core'])
        with patch.object(self.m,'_request',return_value=response):self.assertTrue(self.m.check_for_updates()['has_update'])
        for field,value in [('target_domain','max.max-net.net'),('components',['web','core','l2tp'])]:
            with patch.object(self.m,'_request',return_value={**response,field:value}):self.assertFalse(self.m.check_for_updates()['success'])
    def test_controller_failure_never_means_up_to_date(self):
        with patch.object(self.m,'_request',side_effect=OSError()):
            self.assertFalse(self.m.check_for_updates()['success']);self.assertEqual(self.m.get_update_progress()['status'],'unavailable')
    def test_version_and_backup_cannot_be_overridden(self):
        with patch.object(self.m,'_request') as call:
            for version,backup in [('latest',True),('2.6.4',True),('2.6.3',False)]:self.assertFalse(self.m.trigger_system_update(version,backup)['success'])
            call.assert_not_called()
    def test_apply_only_sends_version_and_mandatory_backup(self):
        with patch.object(self.m,'_authorized',return_value=True),patch.object(self.m,'_request',return_value={'success':True}) as call:
            self.assertTrue(self.m.trigger_system_update('2.6.3',True,'fixture')['success'])
            call.assert_called_once_with('POST','apply',{'version':'2.6.3','backup_first':True})
    def test_apply_outside_authorized_request_never_dispatches(self):
        with patch.object(self.m,'_authorized',return_value=False),patch.object(self.m,'_request') as call:
            self.assertFalse(self.m.trigger_system_update('2.6.3')['success']);call.assert_not_called()


if __name__=='__main__':unittest.main()
