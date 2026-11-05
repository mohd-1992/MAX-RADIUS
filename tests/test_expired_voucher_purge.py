"""Safe deferral occurs before the first write, including unusually large cards."""
import unittest
from unittest.mock import patch
from services import expired_voucher_purge as service


class PurgeGuards(unittest.TestCase):
    def test_large_history_is_deferred_without_any_write(self):
        statements=[]
        def query(conn,statement,params=(),fetch=None):
            statements.append(statement)
            self.assertTrue(statement.startswith('SELECT'))
            if statement.startswith('SELECT username'):return []
            if statement.startswith('SELECT id'):return None
            self.assertIn('LIMIT 5001 FOR UPDATE',statement)
            return [{}]*5001
        with patch.object(service,'sql',side_effect=query):
            result=service.purge_details(object(),{'id':1,'username':'fixture'})
        self.assertEqual(result,dict(deferred=True,reason='large_history_requires_batch_purge'))

    def test_shared_name_protects_other_account(self):
        def query(conn,statement,params=(),fetch=None):
            self.assertTrue(statement.startswith('SELECT'))
            if statement.startswith('SELECT username'):return [{'username':'older-fixture-name'}]
            return {'id':7}
        with patch.object(service,'sql',side_effect=query):
            result=service.purge_details(object(),{'id':1,'username':'fixture'})
        self.assertEqual(result,dict(deferred=True,reason='ambiguous_owner'))

    def test_unsettled_history_does_not_write(self):
        def query(conn,statement,params=(),fetch=None):
            self.assertTrue(statement.startswith('SELECT'))
            if statement.startswith('SELECT username'):return []
            if statement.startswith('SELECT id'):return None
            return [{'radacctid':1,'acctstoptime':None}]
        with patch.object(service,'sql',side_effect=query):
            result=service.purge_details(object(),{'id':1,'username':'fixture'})
        self.assertEqual(result,dict(deferred=True,reason='unsettled_or_incomplete_history'))
