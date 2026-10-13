"""Read-only project discovery and explicit isolated verification; never imports the app."""
import argparse
import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / 'docs/generated/source-index.json'
SMOKE = [
    'tests/release_20261010/test_accounting_display.py',
    'tests/release_20261010/test_reset_proxy.py',
    'tests/release_20261010/test_settlement_regressions.py',
]

def emit(value):
    print(json.dumps(value, ensure_ascii=True, indent=2))

def source_files():
    rows = [ROOT/'run.py']
    for folder in ('core', 'services', 'database', 'web', 'deploy/hosted-max'):
        rows.extend((ROOT/folder).rglob('*.py'))
    return sorted({p for p in rows if p.is_file() and '__pycache__' not in p.parts}, key=str)

def literal(node, fallback=None):
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError):
        return fallback

def inventory():
    modules, routes, errors = [], [], []
    for path in source_files():
        raw = path.read_bytes()
        rel = path.relative_to(ROOT).as_posix()
        try:
            tree = ast.parse(raw.decode('utf-8-sig'), filename=rel)
        except (SyntaxError, UnicodeDecodeError) as exc:
            errors.append({'file':rel, 'error':str(exc)})
            continue
        imports = sorted({n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module})
        functions = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            functions.append({'name':node.name, 'line':node.lineno, 'kind':type(node).__name__})
            for deco in getattr(node, 'decorator_list', []):
                if not isinstance(deco, ast.Call) or not isinstance(deco.func, ast.Attribute) or deco.func.attr != 'route':
                    continue
                rule = literal(deco.args[0]) if deco.args else None
                kw = {k.arg:literal(k.value) for k in deco.keywords if k.arg}
                routes.append({'rule':rule, 'blueprint':ast.unparse(deco.func.value),
                    'methods':kw.get('methods', ['GET']), 'endpoint':kw.get('endpoint', node.name),
                    'handler':node.name, 'file':rel, 'line':node.lineno})
        modules.append({'file':rel, 'sha256':hashlib.sha256(raw).hexdigest(), 'imports':imports,
                        'definitions':sorted(functions, key=lambda x:x['line'])})
    return {'format_version':1, 'source':'static AST, not runtime route registration',
            'modules':modules, 'routes':routes, 'parse_errors':errors}

def doctor():
    needed = ('flask', 'pymysql', 'cryptography', 'requests')
    emit({'root':str(ROOT), 'python':sys.version.split()[0],
          'dependencies_available':{m:importlib.util.find_spec(m) is not None for m in needed},
          'env_present':{n:n in os.environ for n in ('DB_TYPE','DB_HOST','DB_NAME','DB_USER','DB_PASSWORD')},
          'git_checkout':(ROOT/'.git').exists(), 'network_checks':'not performed'})

def check_docs():
    broken = []
    encoding_errors = []
    paths = [ROOT/'README.md', ROOT/'AGENTS.md'] + list((ROOT/'docs').rglob('*.md')) + list((ROOT/'.agents').rglob('SKILL.md'))
    paths += list((ROOT/'tests/scenarios').glob('*.md'))
    for p in paths:
        try:
            content = p.read_text(encoding='utf-8')
        except UnicodeDecodeError as exc:
            encoding_errors.append({'file':p.relative_to(ROOT).as_posix(), 'byte':exc.start})
            continue
        for ref in re.findall(r'\[[^\]]*\]\(([^)]+)\)', content):
            ref = ref.strip().strip('<>').split('#')[0]
            if not ref or re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*:', ref):
                continue
            if not (p.parent/unquote(ref)).exists():
                broken.append({'file':p.relative_to(ROOT).as_posix(), 'target':ref})
    emit({'checked_documents':len(paths), 'broken_links':broken, 'encoding_errors':encoding_errors})
    return bool(broken or encoding_errors)

def run_smoke(output):
    results = []
    for rel in SMOKE:
        try:
            p = subprocess.run([sys.executable, str(ROOT/rel)], cwd=ROOT, capture_output=True,
                               text=True, encoding='utf-8', errors='replace', timeout=120)
            results.append({'test':rel, 'returncode':p.returncode, 'stdout':p.stdout, 'stderr':p.stderr})
        except subprocess.TimeoutExpired:
            results.append({'test':rel, 'returncode':124, 'stderr':'Test exceeded 120 seconds'})
    report = {'suite':'smoke-isolated', 'passed':sum(r['returncode']==0 for r in results),
              'failed':sum(r['returncode']!=0 for r in results), 'results':results}
    if output:
        target = Path(output).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    emit({'suite':report['suite'], 'passed':report['passed'], 'failed':report['failed'],
          'tests':[{'test':r['test'], 'returncode':r['returncode']} for r in results], 'report':output})
    if not output and report['failed']:
        emit({'failure_details':[r for r in results if r['returncode']]})
    return bool(report['failed'])

def compare_schema(before, after):
    def flatten(data, prefix='tables'):
        rows = {}
        for key, value in data.items():
            field = prefix+'.'+key
            if isinstance(value, dict):
                rows.update(flatten(value, field))
            else:
                rows[field] = value
        return rows
    a = json.loads(Path(before).read_text(encoding='utf-8-sig'))
    b = json.loads(Path(after).read_text(encoding='utf-8-sig'))
    if not isinstance(a.get('tables'), dict) or not isinstance(b.get('tables'), dict):
        raise ValueError('Schema JSON must contain a tables object, not customer rows')
    a, b = flatten(a['tables']), flatten(b['tables'])
    emit({'added':sorted(b.keys()-a.keys()), 'removed':sorted(a.keys()-b.keys()),
          'changed':sorted(k for k in a.keys() & b.keys() if a[k]!=b[k])})

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('doctor','index','check-docs','check-python','test-plan'):
        sub.add_parser(name)
    search = sub.add_parser('find');search.add_argument('query');search.add_argument('--limit',type=int,default=20)
    run = sub.add_parser('run-tests');run.add_argument('--suite',choices=['smoke'],required=True);run.add_argument('--output')
    compare = sub.add_parser('compare-schema');compare.add_argument('--before',required=True);compare.add_argument('--after',required=True)
    build = sub.add_parser('build-plan');build.add_argument('--tag',required=True)
    args = parser.parse_args()
    if args.command=='doctor':
        doctor()
    elif args.command=='index':
        data=inventory();INDEX.parent.mkdir(parents=True,exist_ok=True);INDEX.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
        emit({'index':INDEX.relative_to(ROOT).as_posix(),'modules':len(data['modules']),'routes':len(data['routes']),'errors':data['parse_errors']})
        return bool(data['parse_errors'])
    elif args.command=='find':
        # Fresh AST lookup avoids stale indexes and never starts application workers.
        data=inventory();q=args.query.casefold();rows=[]
        for m in data['modules']:
            for d in m['definitions']:
                if q in (m['file']+' '+d['name']).casefold():rows.append(dict(file=m['file'],**d))
        rows += [r for r in data['routes'] if q in json.dumps(r).casefold()]
        emit({'query':args.query,'total':len(rows),'results':rows[:max(1,args.limit)]})
    elif args.command=='check-python':
        paths=source_files()+[Path(__file__)];errors=[]
        for p in paths:
            try:ast.parse(p.read_text(encoding='utf-8-sig'),filename=str(p))
            except (SyntaxError,UnicodeDecodeError) as e:errors.append({'file':p.relative_to(ROOT).as_posix(),'error':str(e)})
        emit({'checked':len(paths),'syntax_errors':errors});return bool(errors)
    elif args.command=='check-docs':
        return check_docs()
    elif args.command=='test-plan':
        emit({'smoke':SMOKE,'integration_manual':['tests/release_20261010/test_voucher_sales_concurrency.py'],
              'live':'tests/scenarios/final-system-check.md','default':'No discovery or live connections'})
    elif args.command=='run-tests':
        return run_smoke(args.output)
    elif args.command=='compare-schema':
        compare_schema(args.before,args.after)
    elif args.command=='build-plan':
        if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}',args.tag):raise ValueError('Invalid Docker tag')
        emit({'execute':False,'cwd':str(ROOT),'commands':[
              ['docker','build','-t','max-radius-web:'+args.tag,'.'],
              ['docker','build','-t','max-radius-core:'+args.tag,'docker/freeradius']],
              'deployment':'Select target project/service and follow docs/DEPLOYMENT.md; no automatic push or restart'})
    return 0

if __name__=='__main__':
    raise SystemExit(main())
