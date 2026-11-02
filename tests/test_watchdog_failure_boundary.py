"""A temporary DB error in the reset check must not kill the watchdog loop."""
import ast
import datetime
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch


class WatchdogBoundary(unittest.TestCase):
    def test_reset_database_unavailable_is_cycle_error(self):
        source=Path(__file__).resolve().parents[1]/'core/watchdog.py'
        tree=ast.parse(source.read_text(encoding='utf-8'))
        fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='run_watchdog_cycle')
        module=types.ModuleType('services.factory_reset_service')
        def unavailable():raise RuntimeError('synthetic database unavailable')
        module.factory_reset_active=unavailable
        scope={'os':types.SimpleNamespace(environ={}), 'datetime':datetime,'print':lambda *_:None}
        exec(compile(ast.Module(body=[fn],type_ignores=[]),str(source),'exec'),scope)
        with patch.dict(sys.modules,{'services.factory_reset_service':module}):
            self.assertEqual(scope['run_watchdog_cycle']()['error'],'synthetic database unavailable')


if __name__=='__main__':unittest.main()
