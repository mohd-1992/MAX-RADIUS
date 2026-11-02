"""Read-only virtual accounting and session-PDF acceptance on an isolated clone."""
import os, sys, threading, json, copy
from pathlib import Path
from unittest.mock import patch
assert sys.argv[1:] == ['--run'] and os.environ.get('DB_NAME') == 'history266_preview'
real_start = threading.Thread.start
threading.Thread.start = lambda self: None
import services.license_guard_service as lic
active=lambda *a,**k:dict(valid=True,status='active',features={},limits={},plan_tier='Development')
lic.get_active_license_status=active
lic.has_license_feature=lambda *a,**k:True
import core.license_feature_policy as feature
feature.verified_state=active
from web import create_app
app=create_app({'TESTING':True,'SALES_SOURCE_ENRICHMENT':False})
import database.db as db
db._run_with_autoheal=lambda func,*a,**kw:func(*a,**kw)
import services.session_history_service as history
from services.session_display_service import enrich_session_display
from flask import render_template_string
checks=[]
# Native source compatibility: all four real views, without listing accounts.
with app.test_request_context():
    assert history.get_session_history('__absent_session_preview__')['total']==0
checks.append('native-four-view-compatibility')

def quote(value):
    if value is None:return 'NULL'
    if isinstance(value,int):return str(value)
    return "'"+str(value).replace("'","''")+"'"
base=dict(radacctid=1,acctuniqueid='preview-1',acctsessionid='s1',username='session-preview',
 original_username='session-preview',nasipaddress='192.0.2.10',calledstationid='هتسبوت الفرع',
 nasportid='bridge1',framedprotocol='',servicetype='Login-User',acctterminatecause='User-Request',
 acctstarttime='2026-10-26 01:00:00',acctupdatetime='2026-10-26 02:00:00',acctstoptime='2026-10-26 03:00:00',
 acctinputoctets=1024,acctoutputoctets=2048,acctsessiontime=7200,framedipaddress='192.0.2.25',
 callingstationid='02:00:00:00:00:01',source_fallback='',reseller_id=8888,
 framedipv6address='',framedipv6prefix='',framedinterfaceid='',delegatedipv6prefix='')
live=[dict(base,radacctid=i,acctuniqueid=f'preview-{i}',acctsessionid=f's{i}') for i in range(1,54)]
live[1].update(framedprotocol='PPP',servicetype='Framed-User',calledstationid='pppoe-office',nasportid='ether2')
live[2].update(acctupdatetime=None,calledstationid='',acctinputoctets=None,acctoutputoctets=None)
archive=[dict(base)]
retained=[dict(base,acctoutputoctets=4096)]
def relation(rows):
    return '('+' UNION ALL '.join('SELECT '+','.join(f'{quote(v)} AS {k}' for k,v in r.items()) for r in rows)+')'
original=history.history_sql
def virtual(username,owner):
    sql,params=original(username,owner)
    for table,rows in [('radacct_owned',live),('radacct_archive_owned',archive),('wisp_imported_session_history_owned',archive),('wisp_traffic_history_owned',retained)]:
        sql=sql.replace('FROM '+table+' WHERE','FROM '+relation(rows)+' '+table+' WHERE')
    return sql,params
with app.test_request_context(),patch.object(history,'history_sql',side_effect=virtual):
    first=history.get_session_history('session-preview')
    second=history.get_session_history('session-preview',page=2)
    all_rows=history.get_session_history('session-preview',page_size=None)['rows']
    assert first['total']==53 and first['pages']==2 and len(first['rows'])==50 and len(second['rows'])==3
    assert not ({r['acctuniqueid'] for r in first['rows']} & {r['acctuniqueid'] for r in second['rows']})
    chunks=list(history.iter_session_history('session-preview',batch_size=21))
    assert [len(rows) for rows,_,_ in chunks]==[21,21,11]
    assert [r['acctuniqueid'] for rows,_,_ in chunks for r in rows]==[r['acctuniqueid'] for r in all_rows]
    assert len(all_rows)==53
    merged=next(r for r in all_rows if r['acctuniqueid']=='preview-1')
    assert merged['acctoutputoctets']==4096
    unknown=next(r for r in all_rows if r['acctuniqueid']=='preview-3')
    assert unknown['acctupdatetime'] is None and unknown['total_str']=='غير محدد' and unknown['connection_point']=='غير محدد'
    assert 'ether2' in next(r for r in all_rows if r['acctuniqueid']=='preview-2')['connection_point']
    with patch('core.ownership.owner_id',return_value=9999):assert history.get_session_history('session-preview')['total']==0
    with patch('core.ownership.owner_id',return_value=8888):assert history.get_session_history('session-preview')['total']==1
checks.extend(['53-rows-pagination-full-export','four-source-dedup-max-counters','unknown-not-zero','pppoe-interface','owner-history-isolation'])
named=enrich_session_display([dict(base)],lambda sql,params:[dict(address='192.0.2.10',label='راوتر الفرع'),dict(address='192.0.2.10',label='fallback')])
assert named[0]['nas_display_name']=='راوتر الفرع'
checks.append('nas-name-priority')
client=app.test_client()
with client.session_transaction() as s:s['admin_id']=1
from services.subscriber_service import get_subscriber_sessions
for module,path in [('subscribers','/subscribers/987654/sessions.pdf'),('vouchers','/vouchers/active-users/987654/sessions.pdf')]:
    route='web.routes.'+module
    with patch(route+'.query_one',return_value=dict(id=987654,username='session-preview')),patch('services.session_history_service.iter_session_history',return_value=iter([(all_rows,53,0)])) as get_rows,patch('services.pdf_export_service.render_parts',side_effect=lambda parts,*a,**kw:(list(parts),__import__('io').BytesIO(b'%PDF-fixture'))[1]):
        response=client.get(path)
        assert response.status_code==200 and response.headers['Content-Type'].startswith('application/pdf'), (path,response.status_code,response.get_json(silent=True))
        assert response.headers['Cache-Control']=='no-store, private'
        get_rows.assert_called_once_with('session-preview')
    with patch(route+'.query_one',return_value=None):assert client.get(path).status_code==404
    with patch(route+'.has_permission',return_value=False):assert client.get(path).status_code==403
    assert app.test_client().get(path).status_code in (302,401)
checks.append('both-export-routes-auth-permissions-missing-private-full')
# Capture only a synthetic standalone session panel, not real account pages.
with app.test_request_context():
    p=(Path(__file__).resolve().parents[1] / 'web/templates/subscriber_details.html').read_text(encoding='utf-8')
    a=p.index('    <div id="tab-pane-sessions"');b=p.index('    <!-- TAB 4:',a)
    panel=p[a:b].replace('tab-pane hidden','tab-pane')
    shell='''<!doctype html><html dir="rtl" lang="ar" class="theme-light"><head><meta charset="utf-8">{% include 'components/product_brand_head.html' %}<script src="/static/vendor/tailwind/tailwind.js"></script><link rel="stylesheet" href="/static/vendor/fonts/tajawal.css"><style>body{font-family:Tajawal;padding:20px;background:#070b14}.table-responsive{overflow-x:auto}html.theme-light body{background:#f1f5f9}html.theme-light .bg-\\[\\#0b0f19\\]{background:white}html.theme-light .text-white,html.theme-light .text-slate-300{color:#0f172a}html.theme-light .text-slate-400{color:#475569}html.theme-light thead{background:#e2e8f0}html.theme-light a.bg-blue-600{color:white}</style></head><body>'''+panel+'</body></html>'
    import re
    base_styles=''.join('<style>'+s+'</style>' for s in re.findall(r'<style>(.*?)</style>', (Path(__file__).resolve().parents[1] / 'web/templates/base.html').read_text(encoding='utf-8'), re.S))
    shell=shell.replace('</head>',base_styles+'</head>')
    html=render_template_string(shell,subscriber=dict(id=987654,username='session-preview'),sessions=[dict(merged,nas_display_name='راوتر الفرع'),next(r for r in all_rows if r['acctuniqueid']=='preview-2'),unknown],session_history=first)
    Path('/evidence/SESSION-PANEL.private.html').write_text(html)
checks.append('synthetic-session-panel-render')
# Restore thread startup before the existing Playwright PDF renderer.
threading.Thread.start=real_start
with app.test_request_context():
    from flask import render_template
    from services.batch_export_service import render_pdf
    html=render_template('session_history_pdf.html',account=dict(id=987654,username='session-preview'),account_kind='subscriber',sessions=all_rows)
    Path('/evidence/SESSION-ALL.private.pdf').write_bytes(render_pdf(html,app.static_folder))
checks.append('53-session-real-pdf')
Path('/evidence/SESSION-HISTORY.private.json').write_text(json.dumps(dict(passed=True,checks=checks,synthetic=True,writes='none to database'),ensure_ascii=False))
print(json.dumps(dict(passed=True,checks=checks),ensure_ascii=False))
