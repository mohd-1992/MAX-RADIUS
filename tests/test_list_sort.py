"""Pure list ordering contracts, without application/DB imports."""
import unittest
from services.list_sort_service import sort_state, sort_rows, card_order, FIELDS


class ListSortTests(unittest.TestCase):
    def test_invalid_or_injected_settings_fall_back(self):
        for table in FIELDS:
            self.assertEqual(sort_state({'list_sort':'id; DROP TABLE x','list_dir':'desc'},table)['key'],'id')
            self.assertEqual(sort_state({'list_sort':'username','list_dir':'desc;--'},table)['direction'],'desc')

    def test_numeric_money_and_unknown_stay_last_both_directions(self):
        rows=[{'id':1,'balance':'10.00'},{'id':2,'balance':'2.00'},{'id':3,'balance':None},{'id':4,'balance':'0'}]
        for direction, expected in [('asc',[4,2,1,3]),('desc',[1,2,4,3])]:
            result=sort_rows(rows,sort_state({'list_sort':'balance','list_dir':direction},'subscribers'))
            self.assertEqual([r['id'] for r in result],expected)
        self.assertEqual(rows[0]['id'],1)

    def test_consumption_uses_bytes_and_keeps_unavailable_distinct(self):
        rows=[{'id':1,'sort_used_bytes':2000000000},{'id':2,'sort_used_bytes':900},{'id':3,'sort_used_bytes':None}]
        result=sort_rows(rows,sort_state({'list_sort':'consumption','list_dir':'asc'},'cards'))
        self.assertEqual([r['id'] for r in result],[2,1,3])

    def test_stable_ties_and_sort_before_page_slice(self):
        rows=[{'id':i,'package_name':str(i%3)} for i in range(130)]
        result=sort_rows(rows,sort_state({'list_sort':'package','list_dir':'asc'},'cards'))
        self.assertEqual([r['id'] for r in result[:3]],[129,126,123])
        self.assertFalse({r['id'] for r in result[:50]} & {r['id'] for r in result[50:100]})

    def test_dates_online_and_unknown(self):
        rows=[{'id':1,'last_seen_str':'2026-01-01'}, {'id':2,'is_online':True}, {'id':3,'last_seen_str':'لم يتصل بعد'}]
        result=sort_rows(rows,sort_state({'list_sort':'lastseen','list_dir':'desc'},'cards'))
        self.assertEqual([r['id'] for r in result],[2,1,3])

    def test_card_sql_is_allowlisted_and_tied(self):
        for key in FIELDS['cards']:
            for direction in ('asc','desc'):
                state=sort_state({'list_sort':key,'list_dir':direction},'cards')
                sql=card_order(state)
                if sql:
                    self.assertIn(direction.upper(),sql)
                    self.assertTrue(sql.endswith('v.id DESC'))
        self.assertIsNone(card_order(sort_state({'list_sort':'consumption'},'cards')))


if __name__=='__main__': unittest.main()
