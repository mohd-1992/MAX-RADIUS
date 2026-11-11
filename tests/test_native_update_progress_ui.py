import datetime as dt,importlib,json,sys,threading,types
from pathlib import Path

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
        request=types.SimpleNamespace(endpoint='system_update',path='/system-update',args={}),
        system_timezone='Asia/Aden', server_timestamp_ms=int(now.timestamp()*1000),
        server_date_only='2026-11-10', server_time_only='15:00:00', default_coa_port=3799,
        currency_symbol='ريال', network_name='اختبار الواجهة', is_over_quota=False)
    html = env.get_template('tools/system_update.html').render(**context)
    app=Flask('progress-fixture',static_folder=str(root/'web/static'))
    state={'status':'running','percent':16,'stage':'إنشاء النسخة الاحتياطية والتحقق منها — 2/12 خطوة منجزة','steps':[{'id':'requirements','label':'فحص المتطلبات','state':'done'},{'id':'image','label':'التحقق من الصورة','state':'done'},{'id':'backup','label':'إنشاء النسخة الاحتياطية والتحقق منها','state':'active'}]}
    @app.route('/')
    def index():return html
    @app.route('/fixture/api_system_update_progress')
    def progress():return jsonify(state)
    @app.route('/fixture/state',methods=['POST'])
    def change():
        from flask import request
        state.clear();state.update(request.get_json());return jsonify(success=True)
    @app.route('/fixture/<path:rest>')
    @app.route('/api/<path:rest>')
    def other(rest):return jsonify(success=True,has_update=False,status='idle',timezone='Asia/Aden',server_timestamp_ms=int(now.timestamp()*1000))
    server=make_server('127.0.0.1',0,app,threaded=True);threading.Thread(target=server.serve_forever,daemon=True).start();results=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(executable_path='/usr/bin/chromium',args=['--no-sandbox'])
        for light in [False,True]:
            for width in [390,1024,1936]:
                page=browser.new_page(viewport={'width':width,'height':1000});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto('http://127.0.0.1:'+str(server.server_port));page.wait_for_function("typeof startPollingProgress==='function'")
                state.clear();state.update(status='running',percent=16,stage='إنشاء النسخة الاحتياطية والتحقق منها — 2/12 خطوة منجزة',steps=[dict(id='backup',label='إنشاء النسخة الاحتياطية والتحقق منها',state='active')])
                page.evaluate("()=>{window.fixtureAlerts=[];window.Swal={fire:(...args)=>{window.fixtureAlerts.push(args);return {then:()=>{}};}};document.getElementById('updateProgressModal').classList.remove('hidden');startPollingProgress();}")
                if light:page.evaluate("document.documentElement.classList.add('theme-light')")
                page.wait_for_function("document.getElementById('progressLivePct').textContent==='16%'");page.wait_for_timeout(1250)
                assert page.locator('#progressLivePct').inner_text()=='16%'
                assert 'النسخة الاحتياطية' in page.locator('#nativeProgressChecklist').inner_text()
                state.update(percent=41,stage='انتظار تجدد الترخيص قبل التبديل — 5/12 خطوة منجزة',steps=[dict(id='backup',label='إنشاء النسخة الاحتياطية والتحقق منها',state='done'),dict(id='lease',label='انتظار تجدد الترخيص',state='active')])
                page.wait_for_function("document.getElementById('progressLivePct').textContent==='41%'")
                assert 'انتظار تجدد' in page.locator('#progressLiveLabel').inner_text()
                assert page.locator('#nativeProgressChecklist .fa-check').count()==1
                page.screenshot(path='/evidence/progress-'+str(width)+'-'+('light' if light else 'dark')+'.png')
                state.update(status='completed',percent=100,stage='اكتمل التحديث والتحقق',steps=[dict(id='commit',label='اعتماد النتيجة',state='done')])
                page.wait_for_function("document.getElementById('progressLivePct').textContent==='100%'");page.wait_for_timeout(1200)
                assert page.evaluate('window.fixtureAlerts.length')==1
                assert not errors,errors
                results.append(dict(width=width,light=light,actual_progress=True,waiting_does_not_advance=True,completed=True));page.close()
        # A rollback is an error outcome, retaining forward progress and showing its stage.
        page=browser.new_page();page.goto('http://127.0.0.1:'+str(server.server_port));page.wait_for_function("typeof startPollingProgress==='function'")
        state.update(status='rolled_back',percent=66,stage='أعيدت النسخة السابقة',message='أعيدت النسخة السابقة وتحقق تشغيلها',steps=[dict(id='verified',label='التحقق من الرجوع',state='done')])
        page.evaluate("()=>{window.fixtureAlerts=[];window.Swal={fire:(...args)=>{window.fixtureAlerts.push(args);return {then:()=>{}};}};document.getElementById('updateProgressModal').classList.remove('hidden');startPollingProgress();}")
        page.wait_for_function('window.fixtureAlerts.length===1');assert page.evaluate('window.fixtureAlerts[0][2]')=='error';assert page.locator('#progressLivePct').inner_text()=='66%'
        results.append(dict(rollback_does_not_claim_success=True));browser.close()
    server.shutdown();Path('/evidence/PROGRESS-UI.private.json').write_text(json.dumps(results));print(json.dumps({'browser_cases':len(results),'passed':True}))

if __name__=='__main__':main()
