"""FreeRADIUS parser and request-scope contracts; SQL semantics need MariaDB."""
import pathlib
import re
import unittest


class RadiusQuotaQueryScope(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        text = (pathlib.Path(__file__).resolve().parents[1] /
                'docker/freeradius/queries.conf').read_text(encoding='utf-8')
        cls.queries = {
            name: re.search(r'^' + name + r'\s*=\s*"(.*?)"', text, re.M | re.S).group(1)
            for name in ('authorize_check_query', 'authorize_reply_query')
        }

    def test_both_queries_fit_native_config_string_limit(self):
        for name, query in self.queries.items():
            with self.subTest(query=name):
                self.assertLess(len(query.encode('utf-8')), 8192)

    def test_each_union_input_is_scoped_before_materialization(self):
        for name, query in self.queries.items():
            with self.subTest(query=name):
                prefix = query.split(')\\\nSELECT id,', 1)[0]
                actual, summary = prefix.split('UNION ALL SELECT', 1)
                self.assertIn("WHERE a.username='%{SQL-User-Name}'", actual)
                self.assertIn("WHERE v.username='%{SQL-User-Name}'", summary)
                self.assertIn('h.voucher_id=v.id', summary)
                self.assertNotIn('radacct_quota_owned', query)

    def test_admission_and_reply_share_identical_quota_projection(self):
        prefixes = [q.split(')\\\nSELECT id,', 1)[0] for q in self.queries.values()]
        self.assertEqual(prefixes[0], prefixes[1])

    def test_reconciliation_uses_physical_session_identity(self):
        query = self.queries['authorize_check_query']
        cycle = query.split('SELECT 1 FROM wisp_vouchers', 1)[1].split(')cu', 1)[0]
        self.assertEqual(cycle.count('JOIN radacct_owned c'), 2)
        self.assertNotIn('JOIN qa', cycle)
        self.assertIn('b.radacctid=c.radacctid', cycle)


if __name__ == '__main__':
    unittest.main()
