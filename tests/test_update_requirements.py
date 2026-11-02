"""Local enforcement of panel requirements, no application or database import."""
import copy,unittest
from unittest.mock import patch
from services import system_update_requirements as r

class RequirementTests(unittest.TestCase):
    def setUp(self):
        self.value={'engine_protocol':2,'platforms':['linux/amd64'],'schema_revisions':['schema265'],
            'release_kind':'preparation','prepares_engine_protocol':2,'migration_plan':None,
            'rollback':{'policy':'compatible','images':{'web':'mohd777/max-radius-web@sha256:'+'a'*64}}}
        self.config={'schema_revision':'schema265'}
    def validate(self):
        with patch.object(r.platform,'system',return_value='Linux'),patch.object(r.platform,'machine',return_value='x86_64'):
            r.validate(self.value,self.config,{'web'})
    def test_local_supported_requirements(self):self.validate()
    def test_unknown_schema_is_never_inferred_from_app_version(self):
        self.config={}
        self.assertNotIn('schema_revision',r.facts(self.config))
        with self.assertRaises(ValueError):self.validate()
    def test_wrong_platform_and_new_protocol_are_rejected(self):
        self.value['platforms']=['linux/arm64']
        with self.assertRaises(ValueError):self.validate()
        self.value['platforms']=['linux/amd64'];self.value['engine_protocol']=4
        with self.assertRaises(ValueError):self.validate()
    def test_sql_and_forward_only_are_not_falsely_advertised(self):
        self.assertIn('migration',r.CAPABILITIES)
        self.value['migration_plan']={'id':'ddl','sha256':'a'*64}
        with self.assertRaises(ValueError):self.validate()
        self.value['migration_plan']=None;self.value['rollback']={'policy':'forward_only','images':{}}
        with self.assertRaises(ValueError):self.validate()
    def test_unimplemented_preparation_target_is_rejected(self):
        self.value['prepares_engine_protocol']=4
        with self.assertRaises(ValueError):self.validate()
    def test_rollback_repositories_are_role_scoped(self):
        self.value['rollback']['images']['web']='mohd777/max-radius-core@sha256:'+'a'*64
        with self.assertRaises(ValueError):self.validate()

if __name__=='__main__':unittest.main()
