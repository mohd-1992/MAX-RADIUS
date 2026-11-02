"""Exercise scheduler construction without importing workers or connecting to NAS."""
import ast
import datetime
from pathlib import Path
import unittest


class SchedulerDateTests(unittest.TestCase):
    def test_date_dialect_and_rollover(self):
        path = Path(__file__).resolve().parents[1] / 'services/nas_session_settlement.py'
        tree = ast.parse(path.read_text(encoding='utf8'))
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                        and n.name == 'freeze_and_disconnect')
        loop = next(n for n in ast.walk(function) if isinstance(n, ast.For)
                    and any(isinstance(x, ast.Assign) and any(
                        isinstance(t, ast.Name) and t.id == 'clock' for t in x.targets)
                        for x in n.body))
        start = next(i for i, n in enumerate(loop.body) if isinstance(n, ast.Assign)
                     and any(isinstance(t, ast.Name) and t.id == 'clock' for t in n.targets))
        end = next(i for i, n in enumerate(loop.body) if isinstance(n, ast.Expr)
                   and isinstance(n.value, ast.Call) and any(isinstance(x, ast.Constant)
                   and x.value == '/system/scheduler/add' for x in n.value.args))
        code = compile(ast.Module(body=loop.body[start:end + 1], type_ignores=[]), str(path), 'exec')
        cases = [
            ('oct/11/2026', '23:00:00', 'oct/11/2026', '23:03:00'),
            ('oct/11/2026', '23:59:00', 'oct/12/2026', '00:02:00'),
            ('dec/31/2026', '23:59:30', 'jan/01/2027', '00:02:30'),
            ('2026-10-11', '23:59:00', '2026-10-12', '00:02:00'),
            ('2028-02-28', '23:59:00', '2028-02-29', '00:02:00'),
        ]
        for date, clock_time, expected_date, expected_time in cases:
            with self.subTest(date=date, time=clock_time):
                calls = []

                def command(api, menu, words=None):
                    if menu == '/system/clock/print':
                        return [{'date': date, 'time': clock_time}]
                    calls.append((menu, words))
                    return []

                exec(code, {'datetime': datetime, 'command': command, 'api': None, 'token': 'test'})
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0][0], '/system/scheduler/add')
                self.assertEqual(calls[0][1]['start-date'], expected_date)
                self.assertEqual(calls[0][1]['start-time'], expected_time)
                self.assertEqual(calls[0][1]['policy'], 'read,write,test')


if __name__ == '__main__':
    unittest.main()
