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
    resources=dict(cpu_percent=.5,cpu_available=False,cpu_scope_label='خدمات الدومين من سعة معالج الخادم',ram_percent=1.,disk_percent=1.,ram_used_gb=0.,ram_total_gb=8.,disk_used_gb=1.,disk_total_gb=100.,containers={})
    context.update(settings={},metrics=dict(resources=resources,voucher_summary={}),traffic_chart=dict(labels=[],download=[],upload=[]),sales_chart=dict(labels=[],sales=[]),latest_operations=[],system_uptime='اختبار',internet_status={})
    env.globals['can_view_network_totals']=lambda:True
    html=env.get_template('dashboard.html').render(**context)
    assert resources['cpu_scope_label'] not in html
    assert 'setInterval(updateDashboardLiveStats, 3500);' in html
    html=html.replace('setInterval(updateDashboardLiveStats, 3500);','window.fixturePoll=updateDashboardLiveStats; setInterval(updateDashboardLiveStats, 3500);')
    app=Flask('cpu-ui',static_folder=str(root/'web/static'))
    @app.route('/')
    def index():return html
    @app.route('/api/dashboard/live-metrics')
    def metrics():return jsonify(success=True,metrics=dict(resources=resources))
    @app.route('/api/<path:rest>')
    @app.route('/fixture/<path:rest>')
    def other(rest):return jsonify(success=True,has_update=False,notices=[],timezone='Asia/Aden',server_timestamp_ms=int(now.timestamp()*1000))
    server=make_server('127.0.0.1',0,app,threaded=True);threading.Thread(target=server.serve_forever,daemon=True).start();results=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(executable_path='/usr/bin/chromium',args=['--no-sandbox'])
        for light in [False,True]:
            for width in [390,1440]:
                resources.update(cpu_percent=.5,cpu_available=False)
                page=browser.new_page(viewport={'width':width,'height':1100});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto('http://127.0.0.1:'+str(server.server_port));page.wait_for_function("typeof window.fixturePoll==='function'")
                if light:page.evaluate("document.documentElement.classList.add('theme-light')")
                assert page.locator('#stat-cpu-percent').inner_text()=='0.5%'
                for cpu,expected in [(0,'0.00%'),(.12,'0.12%'),(24.75,'24.75%')]:
                    resources.update(cpu_percent=cpu,cpu_available=True);page.evaluate('()=>window.fixturePoll()');page.wait_for_function('(v)=>document.getElementById("stat-cpu-percent").textContent===v',arg=expected)
                    assert page.locator('#stat-cpu-bar').evaluate('e=>e.style.width')==str(cpu)+'%'
                resources.update(cpu_percent=.5,cpu_available=False);page.evaluate('()=>window.fixturePoll()');page.wait_for_function('document.getElementById("stat-cpu-percent").textContent==="0.5%"')
                page.locator('#stat-cpu-percent').scroll_into_view_if_needed();page.screenshot(path='/evidence/cpu-'+str(width)+'-'+('light' if light else 'dark')+'.png')
                assert not errors,errors
                results.append(dict(width=width,light=light,genuine_zero=True,changes_displayed=True,fallback_preserved=True));page.close()
        browser.close()
    server.shutdown();Path('/evidence/CPU-UI.private.json').write_text(json.dumps(results));print(json.dumps({'browser_cases':len(results),'passed':True}))

if __name__=='__main__':main()
