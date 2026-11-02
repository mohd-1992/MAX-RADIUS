"""Authorization and durable interruption tests; no application/database import."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from services import system_update_migrations as m
from services import system_update_requirements as requirements


class PlanAuthorization(unittest.TestCase):
    def setUp(self):
        spec, digest = m.metadata()
        self.plan = {'id':spec['id'],'sha256':digest}
        self.value = {'engine_protocol':3,'platforms':['linux/amd64'],
            'schema_revisions':['features267'],'migration_plan':self.plan,
            'rollback':{'policy':'compatible','images':{
                'web':'mohd777/max-radius-web@sha256:'+'a'*64,
                'core':'mohd777/max-radius-core@sha256:'+'b'*64}},
            'release_kind':'feature','prepares_engine_protocol':None}
    def test_plan_is_fixed_and_never_contains_executable_feed_text(self):
        self.assertEqual(m.validate_plan(self.plan,{'web','core'})['initializer'],'features268_additive_v1')
        for value in ({'id':'free-sql','sha256':self.plan['sha256']},
                      dict(self.plan,sql='DROP DATABASE radius'),
                      dict(self.plan,sha256='0'*64)):
            with self.assertRaises(ValueError):m.validate_plan(value,{'web','core'})
    def test_core_or_web_cannot_be_omitted(self):
        with self.assertRaises(ValueError):m.validate_plan(self.plan,{'web'})
        with self.assertRaises(ValueError):m.validate_plan(self.plan,{'core'})
    def test_only_provisioned_schema_and_protocol_are_accepted(self):
        with patch.object(requirements,'facts',return_value={'engine_protocol':3,'platform':'linux/amd64','schema_revision':'features267'}):
            requirements.validate(self.value,{},['web','core'])
            self.value['engine_protocol']=2
            with self.assertRaises(ValueError):requirements.validate(self.value,{},['web','core'])
        self.value['engine_protocol']=3
        for observed in ('unknown',None,'features268'):
            with patch.object(requirements,'facts',return_value={'engine_protocol':3,'platform':'linux/amd64','schema_revision':observed}):
                with self.assertRaises(ValueError):requirements.validate(self.value,{},['web','core'])
    def test_forward_only_and_unscoped_rollback_references_rejected(self):
        for value in ({'policy':'forward_only','images':{}},
                      {'policy':'compatible','images':{'web':'mohd777/max-radius-core@sha256:'+'a'*64}}):
            self.value['rollback']=value
            with patch.object(requirements,'facts',return_value={'engine_protocol':3,'platform':'linux/amd64','schema_revision':'features267'}):
                with self.assertRaises(ValueError):requirements.validate(self.value,{},['web','core'])
    def test_interrupted_plan_never_reexecutes_sql_or_docker(self):
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory)
            m.write_json(folder/'plan.private.json',{'job':'a'*32})
            m.write_json(folder/'state.json',{'status':'running','switch_attempted':True})
            calls=[]
            result=m.run(folder,lambda *a,**k:calls.append(a))
            self.assertEqual(result['status'],'recovery_required')
            self.assertEqual(calls,[])
    def test_completed_receipt_does_not_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory)
            m.write_json(folder/'plan.private.json',{'job':'a'*32})
            m.write_json(folder/'state.json',{'status':'completed','percent':100})
            calls=[];result=m.run(folder,lambda *a,**k:calls.append(a))
            self.assertEqual(result['status'],'completed');self.assertFalse(calls)
    def test_lost_create_reply_is_reconciled_and_not_created_twice(self):
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory);state={'job':'c'*32};calls=[]
            def api(method,path,body=None):
                calls.append((method,path))
                if method=='POST':raise OSError('Response lost after actual creation')
                return [{'Id':'d'*64,'Names':['/owned'],'Labels':{'max.update.job':state['job']}}]
            with self.assertRaises(OSError):m.create_once(api,folder,state,'web',{},'owned','candidates')
            self.assertEqual(state['candidates']['web']['id'],'d'*64)
            with self.assertRaises(m.UpdateError):m.create_once(api,folder,state,'web',{},'owned','candidates')
            self.assertEqual(sum(method=='POST' for method,_ in calls),1)


if __name__=='__main__':unittest.main()
