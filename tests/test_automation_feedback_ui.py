"""Browser acceptance of the actual automation template/assets with fixture GETs.

No product app, license authority, real tasks or customer records are used.
Run in the release image with /evidence writable and the source at /source.
"""
import datetime as dt
import importlib
import json
from pathlib import Path
import re
import sys
import threading
import types


def main():
    from flask import Flask, jsonify
    from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader
    from playwright.sync_api import sync_playwright
    from werkzeug.serving import make_server
    root = Path('/app') if '--compiled' in sys.argv else Path('/source')
    if '--compiled' in sys.argv:
        package = types.ModuleType('web'); package.__path__ = [str(root/'web')]
        sys.modules['web'] = package
        loader = importlib.import_module('web._embedded_templates').template_loader()
    else:
        loader = FileSystemLoader(root/'web/templates')
    # Render the real shared page, including sidebar, container width and scripts.
    from jinja2 import ChainableUndefined
    env = Environment(loader=loader, autoescape=True, undefined=ChainableUndefined)
    env.globals.update(url_for=lambda endpoint, filename=None, **kw: '/static/'+filename if filename else '/fixture/'+endpoint,
        csrf_token=lambda: 'fixture', has_feature=lambda *a, **kw: True,
        has_permission=lambda *a, **kw: True, can_access_endpoint=lambda *a, **kw: True,
        is_reseller_scope=lambda: False, get_flashed_messages=lambda **kw: [])
    now = dt.datetime.now(dt.timezone.utc)
    context = dict(app_version='2.7.1', app_edition='Enterprise', system_title='MAX RADIUS',
        product_identity={'name':'MAX RADIUS','website':''}, current_manager={'id':1,'username':'fixture','full_name':'اختبار الواجهة','role_code':'superadmin'},
        license_info={'valid':True,'has_license':True,'status':'active','features':{}}, session={'admin_id':1},
        request=types.SimpleNamespace(endpoint='system.automation_rules',path='/automation-rules',args={}),
        system_timezone='Asia/Aden', server_timestamp_ms=int(now.timestamp()*1000),
        server_date_only='2026-11-10', server_time_only='15:00:00', default_coa_port=3799,
        currency_symbol='ريال', network_name='اختبار الواجهة', is_over_quota=False)
    html = env.get_template('automation_rules.html').render(**context)
    app = Flask('automation-feedback-ui', static_folder=str(root/'web/static'))
    stamp = dt.datetime.now(dt.timezone.utc).isoformat()
    schedule = dict(mode='daily', times=['06:00'], days=[], timezone='Asia/Aden', start='2026-11-10', endMode='never')
    tasks = [dict(id='a'*32, revision=1, name='مهمة حذف منتهية باسم طويل لاختبار الالتفاف', kind='delete', enabled=False,
                  params=dict(age=1,batch=5000), options={}, schedule=schedule, next_dates=[], run_count=1)]
    result = dict(message='اكتملت المعالجة؛ '+('السجل المالي وملخص الاستهلاك محفوظان. '*12), success=True, processed=15, deleted_vouchers=15, deletion_complete=True, telegram='not_configured',
                  pending_reasons={'active_account_operation':2}, duration_seconds=3.5)
    data = dict(success=True, tasks=tasks, runs=[dict(id='b'*32, task_id='a'*32, state='success', definition=tasks[0],
                result=result, created_at=stamp, updated_at=stamp)], packages=[], batches=[],
                policy=dict(missed='skip',retention=90,resourcePause=True,notify=True),
                notices=[dict(task_id='a'*32,name=tasks[0]['name'],state='review',message='توجد مهمة مماثلة مفعلة؛ راجع الجدولة.',updated_at=stamp)])
    @app.route('/')
    def index(): return html
    @app.route('/api/<path:rest>')
    def shared_api(rest): return jsonify(success=True, count=0, valid=True, timezone='Asia/Aden', server_timestamp_ms=int(now.timestamp()*1000))
    @app.route('/fixture-run-state/<state>')
    def run_state(state):
        assert state in ('running','success')
        data['runs'][0]['state']=state
        return jsonify(success=True)
    @app.route('/api/automation-tasks')
    def api(): return jsonify(data)
    dashboard = loader.get_source(Environment(), 'dashboard.html')[0]
    dashboard_script = dashboard[dashboard.index('<script>', dashboard.index('automation-space-notices')):]
    dashboard_script = dashboard_script[:dashboard_script.index('</script>')+9]
    @app.route('/dashboard-fixture')
    def dashboard_fixture():
        return '<html lang="ar" dir="rtl"><meta charset="utf-8"><span id="topLiveClockTz">Asia/Aden</span><div id="automation-space-notices"></div><script>window.fixtureToasts=[];window.showToast=(message,type)=>window.fixtureToasts.push({message,type});</script>'+dashboard_script+'</html>'
    @app.route('/api/automation-notices')
    def disk_notices():
        return jsonify(success=True, notices=[dict(task_id='c'*32,kind='delete',state='review',message='fixture-review',updated_at=stamp),
                       dict(task_id='d'*32,kind='alert',state='low',message='المساحة المتاحة 5%؛ الحد المحدد 15%',updated_at=stamp),
                       dict(task_id='e'*32,kind='alert',state='recovered',message='المساحة المتاحة 50%؛ الحد المحدد 15%',updated_at=stamp)])
    server = make_server('127.0.0.1', 0, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path='/usr/bin/chromium', args=['--no-sandbox'])
        for light in (False, True):
            for width in (1936, 1440, 1024, 768, 390):
                page = browser.new_page(viewport={'width':width,'height':1000})
                errors = []; page.on('pageerror', lambda exc: errors.append(str(exc)))
                page.goto('http://127.0.0.1:'+str(server.server_port))
                page.wait_for_selector('.task-card')
                assert page.locator('.task-notice, #automationTaskNotices').count()==0
                assert 'تحتاج مراجعة الجدولة' not in page.locator('.task-card').inner_text()
                page.evaluate("()=>{window.fixtureToasts=[];const realToast=window.showToast;window.showToast=(message,type)=>{window.fixtureToasts.push({message,type});return realToast(message,type);};}")
                page.request.get('http://127.0.0.1:'+str(server.server_port)+'/fixture-run-state/running')
                page.evaluate('()=>refresh()')
                assert page.locator('.task-card.running').count()==1
                page.request.get('http://127.0.0.1:'+str(server.server_port)+'/fixture-run-state/success')
                page.evaluate('()=>refresh()')
                assert page.locator('.task-card.running').count()==0
                assert page.locator('.task-notice, #automationTaskNotices').count()==0
                if light: page.evaluate("document.documentElement.classList.add('theme-light')")
                page.locator('[data-tab="history"]').click()
                button = page.locator('[data-history]').first
                page.locator('#historyPanel').evaluate("el=>el.scrollIntoView({block:'start',inline:'nearest'})")
                bounds = button.evaluate('''el=>{const range=document.createRange();range.selectNodeContents(el);const r=range.getBoundingClientRect(),b=el.getBoundingClientRect();return {text:el.textContent.trim(),fits:r.width<=b.width,button:b.width}}''')
                assert 'التفاصيل' in bounds['text'] and bounds['fits'], bounds
                page.screenshot(path=f'/evidence/history-{width}-'+('light' if light else 'dark')+'.png', full_page=True)
                visibility=button.evaluate("""el=>{const b=el.getBoundingClientRect(),w=el.closest('.table-wrap').getBoundingClientRect(),s=el.querySelector('span').getBoundingClientRect();return {left:s.left,right:s.right,wrapperLeft:w.left,wrapperRight:w.right,visible:s.left>=w.left&&s.right<=w.right,wrapperScroll:el.closest('.table-wrap').scrollLeft}}""")
                assert visibility['visible'],visibility
                assert page.locator('.task-notice, #automationTaskNotices').count()==0
                button.click()
                text = page.locator('#detailContent').inner_text()
                for label in ('العناصر المعالجة', 'الكروت المحذوفة', 'قناة الإرسال غير مهيأة', 'عملية جارية على الحساب', '15', '3.5'):
                    assert label in text, (label, text)
                for raw in ('processed','deleted_vouchers','not_configured','active_account_operation','true'):
                    assert raw not in text, raw
                assert page.locator('#legacyRules').count() == 0
                assert not errors, errors
                shot=f'ui-{width}-'+('light' if light else 'dark')+'.png'
                page.screenshot(path='/evidence/'+shot, full_page=True)
                assert page.evaluate('window.fixtureToasts.length')==0,page.evaluate('window.fixtureToasts')
                results.append(dict(width=width,light=light,details=bounds,visibility=visibility,errors=errors,completed_notifications=0))
                page.close()
        page = browser.new_page()
        page.goto('http://127.0.0.1:'+str(server.server_port)+'/dashboard-fixture')
        page.wait_for_function('window.fixtureToasts.length===1')
        disk_text=page.locator('#automation-space-notices').inner_text()
        assert 'تنبيه مساحة التخزين' in disk_text and '5%' in disk_text
        assert 'fixture-review' not in disk_text and 'GMT' not in disk_text
        assert page.locator('.fa-hard-drive').count()==1
        page.reload();page.wait_for_selector('.fa-hard-drive', state='attached')
        assert page.evaluate('window.fixtureToasts.length')==0
        results.append(dict(dashboard_filters=True,recovery_once=True,arabic_timestamp=True))
        page.close()
        browser.close()
    server.shutdown()
    Path('/evidence/UI-RESULT.private.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
    print(json.dumps({'browser_cases':len(results),'passed':True}))


if __name__ == '__main__': main()
