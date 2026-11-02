"""A later release never masks an unfinished preparation or a retry."""
import unittest
import tempfile
import json
import hashlib
from pathlib import Path
from services.system_update_sequence import gate
from services.system_update_handoff import persist
from services.system_update_runtime import UpdateError


class SequenceGateTests(unittest.TestCase):
    def setUp(self):
        self.offer = dict(success=True, has_update=True, current_version='2.6.7',
                          latest_version='2.6.8', automatic_install=False)

    def test_next_release_waits_for_success(self):
        for state in ('queued', 'running', 'handoff_pending', 'error', 'rolled_back',
                      'recovery_required', 'unknown', 'unavailable'):
            with self.subTest(state=state):
                result = gate(self.offer, dict(status=state, target_version='2.6.7'))
                self.assertFalse(result['has_update'])
                self.assertIsNone(result['latest_version'])
                self.assertNotIn('release_id', result)

    def test_success_or_initial_installation_can_check(self):
        for state in ('completed', 'idle'):
            self.assertIs(gate(self.offer, dict(status=state)), self.offer)

    def test_same_stage_retry_is_not_hidden(self):
        self.assertIs(gate(self.offer, dict(status='error', target_version='2.6.8')), self.offer)

    def test_unknown_progress_is_not_success(self):
        self.assertFalse(gate(self.offer, {})['has_update'])


class AttestedConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.folder = self.root/'jobs'/'j'
        self.folder.mkdir(parents=True)
        self.compose = self.root/'compose.json'
        self.before = json.dumps({'services':{'web':{'image':'new'},'update_controller':{'image':'old'}}}).encode()
        self.compose.write_bytes(self.before)
        (self.folder/'controller-compose-before.private').write_bytes(self.before)
        self.channel = self.root/'update-channel.private.json'
        self.raw = json.dumps({'project':'owned','public_key':'preserve-trust','identity':'owned.example.test'}).encode()
        self.channel.write_bytes(self.raw)
        self.plan = dict(job='j', compose_path=str(self.compose), project='owned',
            controller={'service':'update_controller'}, references={'web':'pinned-bridge'},
            schema_preparation={'verified':True,'schema_revision':'features267'},
            channel_sha256=hashlib.sha256(self.raw).hexdigest())

    def tearDown(self): self.temp.cleanup()

    def test_verified_handoff_records_schema_and_executor_without_rotating_trust(self):
        persist(self.plan, self.folder)
        result = json.loads(self.channel.read_text())
        self.assertEqual(result['schema_revision'],'features267')
        self.assertEqual(result['executor_image'],'pinned-bridge')
        self.assertEqual(result['public_key'],'preserve-trust')

    def test_external_config_edit_is_never_overwritten(self):
        self.channel.write_text('{"operator":"changed"}')
        with self.assertRaises(UpdateError): persist(self.plan, self.folder)
        self.assertEqual(self.channel.read_text(),'{"operator":"changed"}')
        self.assertEqual(self.compose.read_bytes(),self.before)

    def test_different_owner_cannot_receive_schema_stamp(self):
        self.plan['project']='foreign'
        with self.assertRaises(UpdateError): persist(self.plan, self.folder)
        self.assertEqual(self.channel.read_bytes(),self.raw)

    def test_host_controller_gets_stamp_without_becoming_docker_executor(self):
        self.plan.pop('controller')
        from services.system_update_sequence import persist_schema
        persist_schema(self.plan,self.folder)
        result=json.loads(self.channel.read_text())
        self.assertEqual(result['schema_revision'],'features267')
        self.assertNotIn('executor_image',result)


if __name__ == '__main__': unittest.main()
