"""Real report templates/assets with synthetic report-service outputs only.

No application factory, real accounts, mutations or external requests.
The browser mode requires a fixture JSON emitted by run_reports_repair_lab.py.
"""
import datetime as dt
from html.parser import HTMLParser
import json
from pathlib import Path
import sys
import threading
from urllib.parse import parse_qs,urlsplit,urlencode

ROOT=Path(__file__).resolve().parents[1]


def render(template,data,args):
    from flask import Flask,render_template
    from jinja2 import ChainableUndefined
    from services.network_traffic_service import format_bytes
    runtime=Path('/app') if '--compiled' in sys.argv else ROOT
    app=Flask('reports-repair-ui',template_folder=str(runtime/'web/templates'),static_folder=str(runtime/'web/static'))
    if '--compiled' in sys.argv:
        import importlib,types
        package=types.ModuleType('web');package.__path__=[str(runtime/'web')];sys.modules['web']=package
        app.jinja_loader=importlib.import_module('web._embedded_templates').template_loader()
    app.jinja_env.undefined=ChainableUndefined
    app.add_url_rule('/sales-reports',endpoint='sales_reports',view_func=lambda:'')
    app.add_url_rule('/export-sales',endpoint='export_sales',view_func=lambda:'')
    def url_for(endpoint,**values):
        if endpoint=='static':return '/static/'+values.pop('filename')+('?' + urlencode(values) if values else '')
        path='/sales-reports' if endpoint=='sales_reports' else '/export-sales' if endpoint=='export_sales' else '/fixture-link/'+endpoint
        return path+('?' + urlencode(values) if values else '')
    app.jinja_env.globals.update(csrf_token=lambda:'fixture',has_feature=lambda *a,**k:True,
        has_permission=lambda *a,**k:True,can_access_endpoint=lambda *a,**k:True,
        is_reseller_scope=lambda:False,can_view_network_totals=lambda:True,traffic_bytes=format_bytes,url_for=url_for)
    common=dict(app_version='2.7.1',app_edition='Enterprise',system_title='MAX RADIUS',
        product_identity=dict(name='MAX RADIUS',website=''),current_manager=dict(id=1,username='fixture',full_name='اختبار التقارير',role_code='superadmin'),
        license_info=dict(valid=True,has_license=True,status='active',features={}),
        system_timezone='Asia/Aden',server_timestamp_ms=1794301200000,server_date_only='2026-11-10',server_time_only='15:00:00',
        default_coa_port=3799,currency_symbol='ريال',network_name='اختبار التقارير',is_over_quota=False)
    with app.test_request_context('/sales-reports',query_string=args):
        return render_template(template,**common,**data)


def check_navigation(html,args):
    class Links(HTMLParser):
        def __init__(self):super().__init__();self.urls=[]
        def handle_starttag(self,tag,attrs):
            values=dict(attrs)
            if tag=='a' and 'href' in values:self.urls.append(values['href'])
    parser=Links();parser.feed(html)
    targets=[parse_qs(urlsplit(url).query) for url in parser.urls if urlsplit(url).path=='/sales-reports']
    traffic=next(p for p in targets if p.get('report_view')==['traffic'])
    switch=next(p for p in targets if p.get('record_kind')==['voucher'])
    for target in (traffic,switch):
        for key in ('period','date_from','date_to','nas','service_type'):
            assert target.get(key)==[args[key]],(key,target)
        assert 'page' not in target


def main():
    if len(sys.argv) not in (3,4) or (len(sys.argv)==4 and sys.argv[3]!='--compiled'):raise SystemExit('Requires synthetic fixtures JSON and evidence directory; optional --compiled')
    from flask import Flask,Response
    from playwright.sync_api import sync_playwright
    from werkzeug.serving import make_server
    import logging
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    cases=json.loads(Path(sys.argv[1]).read_text(encoding='utf8'));out=Path(sys.argv[2]);out.mkdir(exist_ok=True)
    app=Flask('reports-repair-browser',static_folder=str(Path('/app/web/static') if '--compiled' in sys.argv else ROOT/'web/static'))
    html={c['name']:render(c['template'],c['data'],c['args']) for c in cases}
    @app.get('/fixture/<name>')
    def page(name):return Response(html[name],mimetype='text/html')
    @app.get('/api/<path:name>')
    def api(name):
        if name=='dashboard/live-metrics':return dict(success=True,metrics=next(c['data']['metrics'] for c in cases if c['template']=='dashboard.html'))
        return dict(success=True,valid=True,count=0,port=3799,total_nas=0,online_nas=0,notices=[],timezone='Asia/Aden',server_timestamp_ms=1794301200000)
    server=make_server('127.0.0.1',0,app,threaded=True);threading.Thread(target=server.serve_forever,daemon=True).start()
    results=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(executable_path='/usr/bin/chromium',args=['--no-sandbox'])
        for case in cases:
            for light in (False,True):
                for width in (1440,390):
                    page=browser.new_page(viewport=dict(width=width,height=1000));errors=[]
                    page.on('pageerror',lambda error:errors.append(str(error)))
                    origin='http://127.0.0.1:'+str(server.server_port)
                    page.route('**/*',lambda route:route.continue_() if route.request.url.startswith(origin) else route.abort())
                    page.add_init_script("localStorage.setItem('wisp_theme','"+('light' if light else 'dark')+"')")
                    page.goto(origin+'/fixture/'+case['name']);page.wait_for_selector('#salesChart' if case['template']=='dashboard.html' else 'table:visible');page.wait_for_timeout(700)
                    if case['template']=='dashboard.html':
                        value=page.evaluate("()=>{const c=Chart.getChart(document.getElementById('salesChart'));return c.data.datasets[0].data[c.data.labels.indexOf('أكتوبر')];}")
                        assert value==6100,value
                    else:assert page.locator('table').count()>0
                    text=page.locator('body').inner_text()
                    if case['name'] in ('invoice-unknown','measured-partial'):assert 'غير متاح' in text
                    assert 'جزئي' not in text and 'المعلومة فقط' not in text and 'غير متاحة بدقة' not in text
                    if case['template']=='sales_reports.html':
                        assert 'Hotspot / PPPoE (الباقة)' in page.locator('body').inner_text()
                        check_navigation(page.content(),case['args'])
                    if case['name']=='measured-partial':
                        assert 'جلسة دون Stop' not in page.locator('body').inner_text()
                    if case['name']=='resolved-hotspot':
                        assert page.locator('tbody tr').filter(has_text='Hotspot / HS').count()==1
                        assert page.locator('select[name=server] option').filter(has_text='Hotspot / HS').count()==1
                    if case['template']=='network_traffic.html':
                        colors=page.evaluate("()=>['basis','nas'].map(name=>{const s=getComputedStyle(document.querySelector('select[name='+name+']'));return [s.backgroundColor,s.color];})")
                        assert colors[0]==colors[1],colors
                    assert not errors,errors
                    overflow=page.evaluate('document.documentElement.scrollWidth>innerWidth+1')
                    assert not overflow,(case['name'],width,'page overflow')
                    name=f"{case['name']}-{'light' if light else 'dark'}-{width}"
                    page.screenshot(path=str(out/(name+'.png')),full_page=True)
                    results.append(dict(case=case['name'],light=light,width=width,errors=errors,overflow=overflow));page.close()
        browser.close()
    server.shutdown();(out/'browser-results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps(dict(browser_cases=len(results),failures=0,synthetic_only=True)))


if __name__=='__main__':main()
